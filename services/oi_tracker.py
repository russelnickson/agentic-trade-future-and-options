"""PCR and OI buildup / unwinding classification for the active expiry."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable, Literal

OptionSide = Literal["CE", "PE"]


class OISignal(str, Enum):
    """Classic price × OI interpretation for a single option contract."""

    LONG_BUILDUP = "Long Buildup"  # price ↑, OI ↑
    SHORT_BUILDUP = "Short Buildup"  # price ↓, OI ↑
    SHORT_COVERING = "Short Covering"  # price ↑, OI ↓
    LONG_UNWINDING = "Long Unwinding"  # price ↓, OI ↓
    NEUTRAL = "Neutral"  # no meaningful price or OI change


@dataclass(frozen=True)
class StrikeOIState:
    strike: float
    option_type: OptionSide
    ltp: float | None
    oi: int | None
    prev_ltp: float | None = None
    prev_oi: int | None = None
    price_change: float | None = None
    oi_change: int | None = None
    signal: OISignal = OISignal.NEUTRAL

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["signal"] = self.signal.value
        return data


@dataclass(frozen=True)
class PCRSnapshot:
    """Aggregate Call vs Put OI for the active expiry."""

    symbol: str
    expiry: str | None
    call_oi: int
    put_oi: int
    pcr: float | None
    strikes: tuple[StrikeOIState, ...] = field(default_factory=tuple)

    @property
    def total_oi(self) -> int:
        return self.call_oi + self.put_oi

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "expiry": self.expiry,
            "call_oi": self.call_oi,
            "put_oi": self.put_oi,
            "pcr": self.pcr,
            "total_oi": self.total_oi,
            "strikes": [s.to_dict() for s in self.strikes],
        }


def compute_pcr(call_oi: float | int, put_oi: float | int) -> float | None:
    """Put-Call Ratio = total Put OI / total Call OI."""
    call = float(call_oi)
    put = float(put_oi)
    if call <= 0:
        return None
    return put / call


def classify_oi_signal(
    price_change: float | None,
    oi_change: float | None,
    *,
    price_epsilon: float = 0.0,
    oi_epsilon: float = 0.0,
) -> OISignal:
    """
    Classify a strike/side from Δprice and ΔOI.

    | Price | OI | Signal          |
    |-------|----|-----------------|
    | ↑     | ↑  | Long Buildup    |
    | ↓     | ↑  | Short Buildup   |
    | ↑     | ↓  | Short Covering  |
    | ↓     | ↓  | Long Unwinding  |
    """
    if price_change is None or oi_change is None:
        return OISignal.NEUTRAL

    price_up = price_change > price_epsilon
    price_down = price_change < -price_epsilon
    oi_up = oi_change > oi_epsilon
    oi_down = oi_change < -oi_epsilon

    if price_up and oi_up:
        return OISignal.LONG_BUILDUP
    if price_down and oi_up:
        return OISignal.SHORT_BUILDUP
    if price_up and oi_down:
        return OISignal.SHORT_COVERING
    if price_down and oi_down:
        return OISignal.LONG_UNWINDING
    return OISignal.NEUTRAL


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iter_chain_sides(chain: dict[str, Any]) -> Iterable[tuple[float, OptionSide, dict[str, Any]]]:
    strikes = chain.get("strikes") or {}
    for strike_key, sides in strikes.items():
        try:
            strike = float(strike_key)
        except (TypeError, ValueError):
            continue
        if not isinstance(sides, dict):
            continue
        for option_type in ("CE", "PE"):
            side = sides.get(option_type)
            if isinstance(side, dict):
                yield strike, option_type, side  # type: ignore[misc]


class OITracker:
    """
    Tracks per-strike LTP/OI for the active expiry, computes PCR, and labels
    each CE/PE as Long Buildup / Short Buildup / Short Covering / Long Unwinding.
    """

    def __init__(
        self,
        *,
        price_epsilon: float = 0.0,
        oi_epsilon: float = 0.0,
    ) -> None:
        self.price_epsilon = float(price_epsilon)
        self.oi_epsilon = float(oi_epsilon)
        # (symbol, strike, option_type) -> (ltp, oi)
        self._prev: dict[tuple[str, float, OptionSide], tuple[float | None, int | None]] = {}

    def reset(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._prev.clear()
            return
        key_prefix = symbol.strip().upper()
        self._prev = {
            key: value
            for key, value in self._prev.items()
            if key[0] != key_prefix
        }

    def snapshot(self, chain: dict[str, Any]) -> PCRSnapshot:
        """
        Build PCR + strike classifications from a chain-cache style payload.

        Expected shape (from ``ChainCache.get_chain``)::

            {
              "symbol": "NIFTY",
              "expiry": "2026-07-28",
              "strikes": {
                "24500": {"CE": {"ltp": ..., "oi": ...}, "PE": {...}}
              }
            }
        """
        symbol = str(chain.get("symbol") or "").strip().upper() or "UNKNOWN"
        expiry = chain.get("expiry")
        expiry_s = str(expiry) if expiry is not None else None

        call_oi = 0
        put_oi = 0
        strike_states: list[StrikeOIState] = []

        for strike, option_type, side in _iter_chain_sides(chain):
            ltp = _as_float(side.get("ltp"))
            oi = _as_int(side.get("oi"))

            if oi is not None:
                if option_type == "CE":
                    call_oi += oi
                else:
                    put_oi += oi

            key = (symbol, strike, option_type)
            prev_ltp, prev_oi = self._prev.get(key, (None, None))

            price_change = (
                None if ltp is None or prev_ltp is None else ltp - prev_ltp
            )
            oi_change = None if oi is None or prev_oi is None else oi - prev_oi
            signal = classify_oi_signal(
                price_change,
                oi_change,
                price_epsilon=self.price_epsilon,
                oi_epsilon=self.oi_epsilon,
            )

            strike_states.append(
                StrikeOIState(
                    strike=strike,
                    option_type=option_type,
                    ltp=ltp,
                    oi=oi,
                    prev_ltp=prev_ltp,
                    prev_oi=prev_oi,
                    price_change=price_change,
                    oi_change=oi_change,
                    signal=signal,
                )
            )

            # Advance baseline for the next tick/snapshot.
            self._prev[key] = (ltp, oi)

        strike_states.sort(key=lambda s: (s.strike, s.option_type))
        return PCRSnapshot(
            symbol=symbol,
            expiry=expiry_s,
            call_oi=call_oi,
            put_oi=put_oi,
            pcr=compute_pcr(call_oi, put_oi),
            strikes=tuple(strike_states),
        )

    def summarize_signals(self, snapshot: PCRSnapshot) -> dict[str, int]:
        """Count strikes in each buildup / unwinding bucket."""
        counts = {signal.value: 0 for signal in OISignal}
        for state in snapshot.strikes:
            counts[state.signal.value] += 1
        return counts
