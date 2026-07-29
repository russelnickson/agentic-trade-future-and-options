"""Select ATM ± N option strikes and resolve them to broker security tokens."""

from __future__ import annotations

from dataclasses import dataclass, field

from services.symbol_mapper import ExpiryLike, OptionToken, SymbolMapper


@dataclass(frozen=True)
class ActiveStrikeTokens:
    """ATM-centered strike ladder with Call/Put security tokens."""

    atm_strike: float
    strikes: tuple[float, ...]
    call_tokens: dict[float, OptionToken] = field(default_factory=dict)
    put_tokens: dict[float, OptionToken] = field(default_factory=dict)

    @property
    def tokens(self) -> list[OptionToken]:
        """Flat list of all resolved Call and Put tokens (subscription-ready)."""
        return [*self.call_tokens.values(), *self.put_tokens.values()]


def atm_strike(underlying_price: float, step_size: float = 50) -> float:
    """Nearest exchange strike to the spot/futures price."""
    if step_size <= 0:
        raise ValueError(f"step_size must be positive, got {step_size}")
    return round(underlying_price / step_size) * step_size


def strike_ladder(
    underlying_price: float,
    step_size: float = 50,
    num_strikes: int = 10,
) -> tuple[float, tuple[float, ...]]:
    """Return (ATM, strikes) covering ATM ± num_strikes steps."""
    if num_strikes < 0:
        raise ValueError(f"num_strikes must be >= 0, got {num_strikes}")

    atm = atm_strike(underlying_price, step_size)
    strikes = tuple(
        atm + offset * step_size
        for offset in range(-num_strikes, num_strikes + 1)
    )
    return atm, strikes


def get_active_strike_tokens(
    underlying_price: float,
    step_size: float = 50,
    num_strikes: int = 10,
    *,
    symbol: str,
    expiry: ExpiryLike,
    mapper: SymbolMapper,
    skip_missing: bool = True,
) -> ActiveStrikeTokens:
    """
    Resolve ATM ± num_strikes Call and Put security tokens for an underlying.

    Example (NIFTY, step 50, num_strikes 10): returns CE/PE tokens for
    ATM-500 … ATM … ATM+500.
    """
    atm, strikes = strike_ladder(underlying_price, step_size, num_strikes)
    call_tokens: dict[float, OptionToken] = {}
    put_tokens: dict[float, OptionToken] = {}

    for strike in strikes:
        for option_type, bucket in (("CE", call_tokens), ("PE", put_tokens)):
            try:
                bucket[strike] = mapper.get_option_token(
                    symbol, expiry, strike, option_type
                )
            except KeyError:
                if not skip_missing:
                    raise

    return ActiveStrikeTokens(
        atm_strike=atm,
        strikes=strikes,
        call_tokens=call_tokens,
        put_tokens=put_tokens,
    )
