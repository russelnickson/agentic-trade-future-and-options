"""
Mock dry-run execution lifecycle (no live broker orders).

Simulates:
  1. Fetch / load NSE F&O master contracts
  2. Select ≈ Delta 0.50 (ATM/ITM) NIFTY CE strike via Black–Scholes
  3. Place a paper/mock protected LIMIT order
  4. Log the fill to Redis order audit (JSONL fallback)
  5. Verify unrealized P&L math

Run:
    python -m unittest tests.test_mock_execution -v
    python tests/test_mock_execution.py
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dashboard.components.orders import (
    DEFAULT_LOG_PATH,
    ORDERS_STREAM_KEY,
    append_order_audit,
    load_order_audit,
)
from services.greeks_engine import compute_greeks, years_to_expiry
from services.master_downloader import DATA_DIR, save_masters
from services.order_guard import compute_protected_limit_price, place_protected_limit_order
from services.strike_selector import atm_strike, strike_ladder
from services.symbol_mapper import SymbolMapper

# Synthetic session assumptions for the dry-run (no live quotes required).
MOCK_SPOT = 24_500.0
MOCK_STEP = 50.0
MOCK_IV_PRICE_GUESS = 150.0  # option LTP seed for IV/greeks solve
MOCK_QTY = 65  # NIFTY lot (illustrative)
TARGET_DELTA = 0.50
DELTA_TOLERANCE = 0.08


def _next_thursday(from_day: date | None = None) -> date:
    """Approximate weekly NIFTY expiry (Thursday) for greeks TTE."""
    day = from_day or date.today()
    days_ahead = (3 - day.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return day + timedelta(days=days_ahead)


def _ensure_masters() -> Path:
    """Use cached Zerodha NSE F&O master, or download if missing."""
    path = DATA_DIR / "zerodha_nse_fno.csv"
    if path.exists() and path.stat().st_size > 0:
        return path
    save_masters(DATA_DIR)
    if not path.exists():
        raise FileNotFoundError(f"Master download failed — missing {path}")
    return path


def _select_delta_050_itm_call(
    spot: float,
    expiry: date,
    *,
    step: float = MOCK_STEP,
    option_ltp_seed: float = MOCK_IV_PRICE_GUESS,
) -> tuple[float, float, float]:
    """
    Scan ATM ± ladder and pick the CE strike whose BS delta is closest to 0.50.

    Prefer slightly ITM (strike <= spot) when deltas are tied — classic ~0.50 ITM bias.
    Returns (strike, delta, theoretical_seed_used).
    """
    tte = years_to_expiry(days=max((expiry - date.today()).days, 1))
    _, strikes = strike_ladder(spot, step_size=step, num_strikes=6)

    scored: list[tuple[float, float, float]] = []
    for strike in strikes:
        # Seed premium roughly scales with moneyness for a stable IV solve.
        moneyness = abs(spot - strike) / spot
        seed = max(option_ltp_seed * (1.0 - moneyness * 2.0), 20.0)
        greeks = compute_greeks(
            spot=spot,
            strike=strike,
            tte=tte,
            option_ltp=seed,
            option_type="CE",
        )
        if greeks.delta is None:
            continue
        scored.append((strike, float(greeks.delta), seed))

    if not scored:
        raise RuntimeError("Unable to compute deltas for mock strike ladder")

    # Closest to 0.50; break ties toward ITM (lower strike for calls).
    scored.sort(key=lambda row: (abs(row[1] - TARGET_DELTA), row[0]))
    strike, delta, seed = scored[0]
    return strike, delta, seed


def _paper_pnl(*, side: str, qty: int, entry: float, mark: float) -> float:
    """Unrealized P&L for a long/short option paper fill."""
    if side.upper() == "BUY":
        return (mark - entry) * qty
    if side.upper() == "SELL":
        return (entry - mark) * qty
    raise ValueError(side)


def _redis_or_none():
    try:
        from database.redis_client import RedisClient

        client = RedisClient(host="127.0.0.1", port=6379)
        if client.ping():
            return client
    except Exception:
        return None
    return None


class TestMockExecutionLifecycle(unittest.TestCase):
    """End-to-end paper-trading dry run through core engine modules."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.master_path = _ensure_masters()
        cls.mapper = SymbolMapper(cls.master_path)
        cls.expiry = _next_thursday()
        cls.redis = _redis_or_none()

    def test_01_master_contracts_loaded(self) -> None:
        self.assertTrue(self.master_path.exists())
        self.assertGreater(len(self.mapper), 0)
        self.assertEqual(self.mapper.broker, "zerodha")
        print(
            f"\n[1] Master loaded: {self.master_path.name} "
            f"({len(self.mapper)} option contracts indexed)"
        )

    def test_02_select_delta_050_itm_strike(self) -> None:
        atm = atm_strike(MOCK_SPOT, MOCK_STEP)
        strike, delta, seed = _select_delta_050_itm_call(MOCK_SPOT, self.expiry)
        self.assertAlmostEqual(delta, TARGET_DELTA, delta=DELTA_TOLERANCE)

        # Resolve broker token for the chosen contract when listed.
        try:
            token = self.mapper.get_option_token(
                "NIFTY", self.expiry.isoformat(), strike, "CE"
            )
        except KeyError:
            # Expiry in master may not match synthetic Thursday — fall back to any
            # NIFTY CE at this strike from the index by probing nearby dated keys.
            token = None
            for key, tok in self.mapper._tokens.items():  # noqa: SLF001 — test helper
                sym, exp, k, opt = key
                if sym == "NIFTY" and opt == "CE" and float(k) == float(strike):
                    token = tok
                    self.expiry = exp  # align remaining steps to a real expiry
                    break

        self.assertIsNotNone(token, "Expected at least one NIFTY CE master row near strike")
        self.__class__.selected = {
            "atm": atm,
            "strike": strike,
            "delta": delta,
            "seed_ltp": seed,
            "token": int(token),
            "expiry": (
                self.expiry.isoformat()
                if hasattr(self.expiry, "isoformat")
                else str(self.expiry)
            ),
        }
        print(
            f"[2] Delta≈0.50 CE strike={strike} delta={delta:.4f} "
            f"ATM={atm} token={token} expiry={self.__class__.selected['expiry']}"
        )

    def test_03_place_paper_protected_limit_order(self) -> None:
        sel = getattr(self.__class__, "selected", None)
        self.assertIsNotNone(sel, "Strike selection must run first")

        ltp = float(sel["seed_ltp"])
        expected_limit = compute_protected_limit_price(ltp, "BUY", slippage=0.50)
        result = place_protected_limit_order(
            symbol=f"NIFTY-{sel['strike']}-CE",
            action="BUY",
            qty=MOCK_QTY,
            ltp=ltp,
            security_id=sel["token"],
            dry_run=True,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.request.order_type, "LIMIT")
        self.assertEqual(result.limit_price, expected_limit)
        self.assertEqual(result.order_id, "DRY-RUN")

        self.__class__.paper_order = {
            "order_id": f"PAPER-{sel['token']}-{sel['strike']}",
            "strike": sel["strike"],
            "action": "BUY",
            "qty": MOCK_QTY,
            "ltp": ltp,
            "limit_price": result.limit_price,
            "token": sel["token"],
            "strategy_name": "MOCK_DELTA_050",
        }
        print(
            f"[3] Paper LIMIT BUY qty={MOCK_QTY} ltp={ltp:.2f} → "
            f"limit={result.limit_price:.2f} (slippage +0.50)"
        )

    def test_04_log_order_to_redis_or_file(self) -> None:
        order = getattr(self.__class__, "paper_order", None)
        self.assertIsNotNone(order, "Paper order must exist")

        append_order_audit(
            {
                "order_id": order["order_id"],
                "timestamp": date.today().isoformat(),
                "strategy_name": order["strategy_name"],
                "strike": order["strike"],
                "action": order["action"],
                "quantity": order["qty"],
                "status": "COMPLETE",
                "execution_latency_ms": 12.3,
                "limit_price": order["limit_price"],
                "ltp": order["ltp"],
                "note": "mock dry-run fill",
            },
            redis_client=self.redis,
        )

        rows, source = load_order_audit(self.redis, limit=50)
        matched = [r for r in rows if r.order_id == order["order_id"]]
        self.assertTrue(matched, f"Order {order['order_id']} not found in audit ({source})")
        self.assertEqual(matched[0].status, "COMPLETE")
        self.assertEqual(matched[0].action, "BUY")

        if self.redis is not None:
            self.assertTrue(
                source.startswith("redis:") or source.startswith("file:"),
                source,
            )
            print(f"[4] Logged to {source} (Redis stream `{ORDERS_STREAM_KEY}` preferred)")
        else:
            self.assertTrue(source.startswith("file:"))
            print(f"[4] Redis unavailable — logged to {DEFAULT_LOG_PATH}")

    def test_05_verify_pnl_calculation(self) -> None:
        order = getattr(self.__class__, "paper_order", None)
        self.assertIsNotNone(order, "Paper order must exist")

        entry = float(order["limit_price"])
        # Simulate favorable mark: option rises ₹10 after entry.
        mark = entry + 10.0
        pnl = _paper_pnl(side="BUY", qty=order["qty"], entry=entry, mark=mark)
        expected = (mark - entry) * order["qty"]
        self.assertAlmostEqual(pnl, expected, places=6)
        self.assertGreater(pnl, 0)

        # Adverse move check for symmetry.
        down_mark = entry - 5.0
        loss = _paper_pnl(side="BUY", qty=order["qty"], entry=entry, mark=down_mark)
        self.assertAlmostEqual(loss, (down_mark - entry) * order["qty"], places=6)
        self.assertLess(loss, 0)

        print(
            f"[5] P&L OK — entry={entry:.2f} mark={mark:.2f} qty={order['qty']} "
            f"unrealized=₹{pnl:,.2f} (loss case ₹{loss:,.2f})"
        )


def run_lifecycle() -> None:
    """Convenience CLI runner with prose output."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestMockExecutionLifecycle)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("\nMock dry-run lifecycle completed successfully.")


if __name__ == "__main__":
    run_lifecycle()
