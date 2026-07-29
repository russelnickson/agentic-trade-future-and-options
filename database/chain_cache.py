"""Hot option-chain cache in Redis for NIFTY and BANKNIFTY."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable

from database.redis_client import RedisClient, get_redis_client
from services.strike_selector import ActiveStrikeTokens

SUPPORTED_UNDERLYINGS = frozenset({"NIFTY", "BANKNIFTY"})

_SYMBOL_ALIASES = {
    "NIFTY": "NIFTY",
    "NIFTY 50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "BANK NIFTY": "BANKNIFTY",
    "NIFTY BANK": "BANKNIFTY",
}


def normalize_underlying(symbol: str) -> str:
    key = " ".join(symbol.strip().upper().split())
    try:
        return _SYMBOL_ALIASES[key]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported underlying {symbol!r}; expected one of {sorted(SUPPORTED_UNDERLYINGS)}"
        ) from exc


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_ltp(tick: dict[str, Any]) -> float | None:
    for key in ("ltp", "last_price", "last_traded_price", "LTP"):
        if key in tick and tick[key] is not None:
            return float(tick[key])
    return None


def _extract_volume(tick: dict[str, Any]) -> int | None:
    for key in ("volume", "volume_traded", "volume_traded_today", "Volume"):
        if key in tick and tick[key] is not None:
            return int(tick[key])
    return None


def _extract_oi(tick: dict[str, Any]) -> int | None:
    for key in ("oi", "open_interest", "OI"):
        if key in tick and tick[key] is not None:
            return int(tick[key])
    return None


def _empty_side(token: int | None = None) -> dict[str, Any]:
    return {
        "token": token,
        "ltp": None,
        "volume": None,
        "oi": None,
    }


def _empty_chain(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "underlying_ltp": None,
        "atm": None,
        "expiry": None,
        "updated_at": None,
        "strikes": {},
    }


class ChainCache:
    """
    Maintains per-underlying option-chain JSON in Redis.

    Local mirrors enable O(1) tick patches; each update is written back so
    Redis always holds the latest LTP / volume / OI snapshot.
    """

    def __init__(self, redis_client: RedisClient | None = None) -> None:
        self._redis = redis_client or get_redis_client()
        self._chains: dict[str, dict[str, Any]] = {
            symbol: _empty_chain(symbol) for symbol in SUPPORTED_UNDERLYINGS
        }
        # token -> (symbol, strike_key, option_type)
        self._token_index: dict[int, tuple[str, str, str]] = {}
        self._load_from_redis()

    def _load_from_redis(self) -> None:
        for symbol in SUPPORTED_UNDERLYINGS:
            stored = self._redis.get_option_chain_state(symbol)
            if stored:
                self._chains[symbol] = stored
                self._reindex_symbol(symbol)

    def _reindex_symbol(self, symbol: str) -> None:
        chain = self._chains[symbol]
        for strike_key, sides in chain.get("strikes", {}).items():
            for option_type, side in sides.items():
                token = side.get("token")
                if token is not None:
                    self._token_index[int(token)] = (symbol, str(strike_key), option_type)

    def _persist(self, symbol: str) -> None:
        chain = self._chains[symbol]
        chain["updated_at"] = _utc_now_iso()
        self._redis.update_option_chain_state(symbol, chain)

    def bootstrap(
        self,
        symbol: str,
        active: ActiveStrikeTokens,
        *,
        expiry: str | None = None,
        underlying_ltp: float | None = None,
    ) -> dict[str, Any]:
        """Seed the chain skeleton from ATM ± N strike tokens."""
        symbol = normalize_underlying(symbol)
        strikes: dict[str, Any] = {}

        for strike in active.strikes:
            strike_key = _strike_key(strike)
            strikes[strike_key] = {
                "CE": _empty_side(active.call_tokens.get(strike)),
                "PE": _empty_side(active.put_tokens.get(strike)),
            }

        # Drop stale token index entries for this symbol.
        self._token_index = {
            token: loc
            for token, loc in self._token_index.items()
            if loc[0] != symbol
        }

        self._chains[symbol] = {
            "symbol": symbol,
            "underlying_ltp": underlying_ltp,
            "atm": active.atm_strike,
            "expiry": expiry,
            "updated_at": None,
            "strikes": strikes,
        }
        self._reindex_symbol(symbol)
        self._persist(symbol)
        return deepcopy(self._chains[symbol])

    def on_tick(self, token: int | str, tick: dict[str, Any]) -> bool:
        """
        Apply a tick to the matching CE/PE node (LTP, volume, OI).

        Also refreshes the per-token latest-tick key. Returns True if the
        token belongs to a cached NIFTY/BANKNIFTY chain strike.
        """
        token_i = int(token)
        self._redis.set_latest_tick(token_i, tick)

        loc = self._token_index.get(token_i)
        if loc is None:
            return False

        symbol, strike_key, option_type = loc
        side = self._chains[symbol]["strikes"][strike_key][option_type]

        ltp = _extract_ltp(tick)
        volume = _extract_volume(tick)
        oi = _extract_oi(tick)

        if ltp is not None:
            side["ltp"] = ltp
        if volume is not None:
            side["volume"] = volume
        if oi is not None:
            side["oi"] = oi

        self._persist(symbol)
        return True

    def update_underlying_ltp(self, symbol: str, ltp: float) -> None:
        symbol = normalize_underlying(symbol)
        self._chains[symbol]["underlying_ltp"] = float(ltp)
        self._persist(symbol)

    def get_chain(self, symbol: str) -> dict[str, Any]:
        symbol = normalize_underlying(symbol)
        return deepcopy(self._chains[symbol])

    def get_all_chains(self) -> dict[str, dict[str, Any]]:
        return {symbol: self.get_chain(symbol) for symbol in SUPPORTED_UNDERLYINGS}

    def indexed_tokens(self) -> Iterable[int]:
        return self._token_index.keys()


def _strike_key(strike: float) -> str:
    if float(strike).is_integer():
        return str(int(strike))
    return str(strike)
