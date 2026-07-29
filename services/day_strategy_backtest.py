"""Day-strategy backtester — top strategies for the session with confidence scores.

Uses Dhan daily OHLC (+ India VIX when available) as an honest proxy for
index F&O day structures. Results are statistical backtests, not guarantees.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HISTORY_DIR = PROJECT_ROOT / "data" / "history"
OUT_DIR = PROJECT_ROOT / "data" / "insights"
SNAPSHOT_PATH = OUT_DIR / "top_strategies.json"
REDIS_KEY = "agent:strategies:today"

StrategyFn = Callable[[pd.DataFrame], pd.Series]


@dataclass
class StrategySpec:
    strategy_id: str
    name: str
    structure: str
    thesis: str
    when_to_use: str
    invalidate: str
    evaluate: StrategyFn = field(repr=False)


@dataclass
class StrategyResult:
    rank: int
    strategy_id: str
    name: str
    structure: str
    thesis: str
    when_to_use: str
    invalidate: str
    confidence: float
    win_rate: float
    trades: int
    avg_pnl_r: float
    expectancy_r: float
    profit_factor: float | None
    max_dd_r: float
    regime_n: int
    regime_win_rate: float
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_daily(symbol: str) -> pd.DataFrame:
    path = HISTORY_DIR / f"{symbol.lower()}_daily.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"Missing history: {path}")
    df = pd.read_parquet(path).copy()
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.sort_values("time").reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            raise ValueError(f"{path.name} missing {col}")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    return df


def _load_vix() -> pd.DataFrame | None:
    path = HISTORY_DIR / "india_vix_daily.parquet"
    if not path.is_file():
        return None
    vix = pd.read_parquet(path)
    if "time" not in vix.columns or "close" not in vix.columns:
        return None
    vix = vix[["time", "close"]].copy()
    vix["time"] = pd.to_datetime(vix["time"], utc=True)
    vix["vix"] = pd.to_numeric(vix["close"], errors="coerce")
    return vix.dropna(subset=["vix"]).sort_values("time")


def _enrich(df: pd.DataFrame, vix: pd.DataFrame | None) -> pd.DataFrame:
    out = df.copy()
    out["ret"] = out["close"].pct_change()
    out["intra_ret"] = out["close"] / out["open"] - 1.0
    out["range_pct"] = (out["high"] - out["low"]) / out["open"]
    out["gap"] = out["open"] / out["close"].shift(1) - 1.0
    out["prior_high"] = out["high"].shift(1)
    out["prior_low"] = out["low"].shift(1)
    out["prior_close"] = out["close"].shift(1)
    out["ma20"] = out["close"].rolling(20).mean()
    out["ma50"] = out["close"].rolling(50).mean()
    out["vol20"] = out["ret"].rolling(20).std() * np.sqrt(252)
    out["trend_up"] = out["close"] > out["ma20"]
    if vix is not None and not vix.empty:
        merged = pd.merge_asof(
            out.sort_values("time"),
            vix[["time", "vix"]].sort_values("time"),
            on="time",
            direction="backward",
        )
        out = merged
        out["vix_chg"] = out["vix"].pct_change()
    else:
        out["vix"] = np.nan
        out["vix_chg"] = np.nan
    return out


def _strategies() -> list[StrategySpec]:
    def iron_fly(df: pd.DataFrame) -> pd.Series:
        # Proxy: tight range day favors short-vol iron fly
        # +1 if range small and close near open; -1 if wide expansion day
        win = (df["range_pct"] <= 0.006) & (df["intra_ret"].abs() <= 0.0035)
        loss = df["range_pct"] >= 0.012
        out = pd.Series(0.0, index=df.index)
        out = out.mask(win, 0.55)
        out = out.mask(loss, -1.0)
        # mild win for medium-tight
        mid = (~win) & (~loss) & (df["range_pct"] <= 0.009)
        out = out.mask(mid, 0.15)
        return out

    def bull_put(df: pd.DataFrame) -> pd.Series:
        # Support holds: low stays above prior_low*0.998 and close >= open
        hold = df["low"] >= df["prior_low"] * 0.998
        green = df["close"] >= df["open"]
        win = hold & green
        loss = df["close"] < df["prior_low"]
        out = pd.Series(0.0, index=df.index)
        out = out.mask(win, 0.45)
        out = out.mask(loss, -1.0)
        soft = hold & ~green & (df["close"] >= df["prior_close"])
        out = out.mask(soft, 0.15)
        return out

    def bear_call(df: pd.DataFrame) -> pd.Series:
        hold = df["high"] <= df["prior_high"] * 1.002
        red = df["close"] <= df["open"]
        win = hold & red
        loss = df["close"] > df["prior_high"]
        out = pd.Series(0.0, index=df.index)
        out = out.mask(win, 0.45)
        out = out.mask(loss, -1.0)
        soft = hold & ~red & (df["close"] <= df["prior_close"])
        out = out.mask(soft, 0.15)
        return out

    def breakout_long(df: pd.DataFrame) -> pd.Series:
        brk = df["close"] > df["prior_high"]
        follow = df["close"] > df["open"]
        win = brk & follow
        loss = brk & (df["close"] < df["open"])  # failed breakout
        out = pd.Series(np.nan, index=df.index)
        # only score on breakout attempts
        out = out.mask(win, 0.8)
        out = out.mask(loss, -1.0)
        return out

    def fade_gap_up(df: pd.DataFrame) -> pd.Series:
        gap_up = df["gap"] >= 0.003
        fade = df["close"] < df["open"]
        win = gap_up & fade
        loss = gap_up & (df["close"] > df["open"] * 1.002)
        out = pd.Series(np.nan, index=df.index)
        out = out.mask(win, 0.6)
        out = out.mask(loss, -1.0)
        soft = gap_up & ~win & ~loss
        out = out.mask(soft, -0.2)
        return out

    def buy_dip(df: pd.DataFrame) -> pd.Series:
        # Weak open / early pressure then reclaim
        weak_open = df["gap"] <= -0.0015
        reclaim = df["close"] > ((df["high"] + df["low"]) / 2.0)
        win = weak_open & reclaim & (df["close"] >= df["open"])
        loss = weak_open & (df["close"] < df["prior_low"])
        out = pd.Series(np.nan, index=df.index)
        out = out.mask(win, 0.7)
        out = out.mask(loss, -1.0)
        soft = weak_open & ~win & ~loss
        out = out.mask(soft, -0.15)
        return out

    def trend_ma(df: pd.DataFrame) -> pd.Series:
        long_bias = df["trend_up"] & (df["close"] >= df["prior_close"])
        fail = df["trend_up"] & (df["close"] < df["ma20"] * 0.995)
        out = pd.Series(0.0, index=df.index)
        out = out.mask(long_bias, 0.35)
        out = out.mask(fail, -0.8)
        # short side when below MA20
        short_bias = (~df["trend_up"]) & (df["close"] <= df["prior_close"])
        out = out.mask(short_bias, 0.35)
        return out

    def vol_crush_fly(df: pd.DataFrame) -> pd.Series:
        # Falling VIX + contained range
        if df["vix_chg"].isna().all():
            return pd.Series(np.nan, index=df.index)
        crush = df["vix_chg"] <= -0.02
        tight = df["range_pct"] <= 0.008
        win = crush & tight
        loss = crush & (df["range_pct"] >= 0.014)
        out = pd.Series(np.nan, index=df.index)
        out = out.mask(win, 0.65)
        out = out.mask(loss, -1.0)
        soft = crush & ~win & ~loss
        out = out.mask(soft, 0.1)
        return out

    return [
        StrategySpec(
            "iron_fly_range",
            "ATM Iron Fly / Range Harvest",
            "Short ATM straddle hedged with wings (or tight iron condor)",
            "Harvest premium when realized range stays tight vs entry IV.",
            "Neutral PCR, contained Asia/GIFT, no event shock",
            "Gap > 0.6% or range expands beyond wing width",
            iron_fly,
        ),
        StrategySpec(
            "bull_put_credit",
            "Bull Put Credit @ Support",
            "Sell put spread above support / strong shopping zone",
            "Bullish-to-neutral: collect credit while support holds.",
            "Trend/support bias POSITIVE; spot above support zone",
            "Daily close below support / prior swing low",
            bull_put,
        ),
        StrategySpec(
            "bear_call_credit",
            "Bear Call Credit @ Resistance",
            "Sell call spread below resistance / profit-booking zone",
            "Fade extension into upper/booking zone with defined risk.",
            "Spot into upper zone; sentiment stretched",
            "Close above resistance / prior swing high",
            bear_call,
        ),
        StrategySpec(
            "breakout_long",
            "Momentum Breakout (CE / Fut long proxy)",
            "Debit call spread or future long on confirmed breakout",
            "Follow strength when close clears prior high with follow-through.",
            "Global/trend POSITIVE; breakout volume/OI confirm",
            "Failed breakout — close back inside prior range",
            breakout_long,
        ),
        StrategySpec(
            "fade_gap_up",
            "Fade Gap-Up Open",
            "Bear call / short futures on gap-up that fails",
            "Gap-ups often mean-revert when F&O is neutral and overnight news thin.",
            "Gap ≥ 0.3% with weak follow-through",
            "Gap-and-go: price holds above open and prior high",
            fade_gap_up,
        ),
        StrategySpec(
            "buy_dip_reclaim",
            "Buy-the-Dip Reclaim",
            "Bull put / debit put buy on reclaim after weak open",
            "Weak open that reclaims mid/high of day favors dip-buy credit/debit.",
            "Support nearby; DII/positive trend backdrop",
            "Close below prior low / strong shopping zone",
            buy_dip,
        ),
        StrategySpec(
            "ma_trend_follow",
            "MA20 Trend Follow",
            "Directional debit spreads with MA20 bias",
            "Stay with the 20-day trend until a decisive reclaim/reject.",
            "Clear MA20 slope; avoid chop around MA",
            "Close through MA20 against the bias",
            trend_ma,
        ),
        StrategySpec(
            "vix_crush_fly",
            "India VIX Crush + Iron Fly",
            "Short-vol fly when VIX is falling",
            "Falling India VIX with contained range is classic premium harvest.",
            "VIX down ≥2% day-over-day; no macro print",
            "VIX reverse spike or range blowout",
            vol_crush_fly,
        ),
    ]


def _score_series(pnl: pd.Series) -> dict[str, float]:
    s = pnl.dropna()
    if s.empty:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "avg_pnl_r": 0.0,
            "expectancy_r": 0.0,
            "profit_factor": None,
            "max_dd_r": 0.0,
        }
    wins = s[s > 0]
    losses = s[s < 0]
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float((-losses).sum()) if len(losses) else 0.0
    pf = (gp / gl) if gl > 0 else (None if gp == 0 else 99.0)
    equity = s.cumsum()
    dd = float((equity - equity.cummax()).min()) if len(equity) else 0.0
    return {
        "trades": int(len(s)),
        "win_rate": float((s > 0).mean()),
        "avg_pnl_r": float(s.mean()),
        "expectancy_r": float(s.mean()),
        "profit_factor": None if pf is None else float(min(pf, 99.0)),
        "max_dd_r": float(dd),
    }


def _regime_mask(df: pd.DataFrame) -> pd.Series:
    """Similarity mask vs latest bar (vol + trend regime)."""
    last = df.iloc[-1]
    vol = last.get("vol20")
    trend = bool(last.get("trend_up"))
    mask = pd.Series(True, index=df.index)
    if pd.notna(vol) and float(vol) > 0:
        mask &= (df["vol20"] - float(vol)).abs() <= max(0.04, float(vol) * 0.45)
    mask &= df["trend_up"] == trend
    # need enough peers
    if int(mask.sum()) < 40:
        mask = (df["trend_up"] == trend)
    if int(mask.sum()) < 40:
        mask = pd.Series(True, index=df.index)
    return mask


def _confidence(
    *,
    full: dict[str, float],
    regime: dict[str, float],
    bias_boost: float,
) -> float:
    """0–1 confidence from sample size, win rate, regime agreement, expectancy."""
    n = full["trades"]
    wr = full["win_rate"]
    exp = full["expectancy_r"]
    rn = regime["trades"]
    rwr = regime["win_rate"]

    sample = min(1.0, n / 180.0)
    wr_term = max(0.0, min(1.0, (wr - 0.35) / 0.35))  # 35%→0, 70%→1
    exp_term = max(0.0, min(1.0, (exp + 0.2) / 0.5))
    regime_term = 0.5
    if rn >= 20:
        regime_term = max(0.0, min(1.0, (rwr - 0.35) / 0.35))
    conf = (
        0.28 * sample
        + 0.32 * wr_term
        + 0.20 * exp_term
        + 0.15 * regime_term
        + 0.05 * max(0.0, min(1.0, bias_boost))
    )
    return float(round(max(0.05, min(0.95, conf)), 3))


def _bias_boost(strategy_id: str, signals: dict[str, str] | None) -> float:
    if not signals:
        return 0.4
    pos = sum(1 for v in signals.values() if str(v).upper() == "POSITIVE")
    neu = sum(1 for v in signals.values() if str(v).upper() == "NEUTRAL")
    bullish_sheet = pos >= 3
    if strategy_id in {"bull_put_credit", "breakout_long", "buy_dip_reclaim", "ma_trend_follow"} and bullish_sheet:
        return 0.9
    if strategy_id in {"iron_fly_range", "vix_crush_fly"} and neu >= 2:
        return 0.75
    if strategy_id in {"bear_call_credit", "fade_gap_up"} and bullish_sheet:
        return 0.25
    return 0.5


def run_day_strategies(
    symbol: str = "NIFTY",
    *,
    top_n: int = 5,
    signals: dict[str, str] | None = None,
) -> dict[str, Any]:
    symbol_u = symbol.strip().upper()
    df = _enrich(_load_daily(symbol_u), _load_vix())
    regime = _regime_mask(df)
    rows: list[StrategyResult] = []

    for spec in _strategies():
        try:
            pnl = spec.evaluate(df)
        except Exception:
            logger.exception("Strategy %s failed", spec.strategy_id)
            continue
        # skip all-NaN (e.g. VIX strategy without data)
        if pnl.dropna().empty:
            continue
        full = _score_series(pnl)
        reg = _score_series(pnl[regime])
        boost = _bias_boost(spec.strategy_id, signals)
        conf = _confidence(full=full, regime=reg, bias_boost=boost)
        # blend score for ranking: confidence + mild expectancy
        rank_score = conf * 0.75 + max(0.0, min(1.0, (full["expectancy_r"] + 0.25) / 0.6)) * 0.25
        rows.append(
            (
                rank_score,
                StrategyResult(
                    rank=0,
                    strategy_id=spec.strategy_id,
                    name=spec.name,
                    structure=spec.structure,
                    thesis=spec.thesis,
                    when_to_use=spec.when_to_use,
                    invalidate=spec.invalidate,
                    confidence=conf,
                    win_rate=round(full["win_rate"], 4),
                    trades=full["trades"],
                    avg_pnl_r=round(full["avg_pnl_r"], 4),
                    expectancy_r=round(full["expectancy_r"], 4),
                    profit_factor=None
                    if full["profit_factor"] is None
                    else round(float(full["profit_factor"]), 3),
                    max_dd_r=round(full["max_dd_r"], 4),
                    regime_n=reg["trades"],
                    regime_win_rate=round(reg["win_rate"], 4),
                    notes=(
                        f"OHLC proxy backtest on {symbol_u} daily · "
                        f"regime peers {reg['trades']} (trend/vol matched) · "
                        f"win {full['win_rate']:.0%} / regime win {reg['win_rate']:.0%}"
                    ),
                ),
            )
        )

    rows.sort(key=lambda x: x[0], reverse=True)
    top = []
    for i, (_score, res) in enumerate(rows[:top_n], start=1):
        res.rank = i
        top.append(res)

    last = df.iloc[-1]
    payload = {
        "asof": datetime.utcnow().isoformat() + "Z",
        "symbol": symbol_u,
        "history_bars": int(len(df)),
        "last_close": float(last["close"]),
        "vol20": None if pd.isna(last.get("vol20")) else float(last["vol20"]),
        "trend_up": bool(last.get("trend_up")),
        "signals_used": signals or {},
        "disclaimer": (
            "Backtests use daily OHLC proxies for F&O day structures. "
            "Confidence is statistical — not a promise of profit. Confirm with live PCR/IV."
        ),
        "strategies": [r.to_dict() for r in top],
        "all_ranked": [r.to_dict() for _, r in rows],
    }
    return payload


def persist_strategies(
    payload: dict[str, Any],
    *,
    redis_client: Any | None = None,
) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"top_strategies_{payload.get('symbol', 'NIFTY').lower()}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    SNAPSHOT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if redis_client is not None:
        try:
            redis_client.client.set(REDIS_KEY, json.dumps(payload, default=str))
            redis_client.client.set(
                f"{REDIS_KEY}:{payload.get('symbol', 'NIFTY')}",
                json.dumps(payload, default=str),
            )
        except Exception:
            logger.debug("Redis strategy persist failed", exc_info=True)
    return path


def load_strategies(
    symbol: str = "NIFTY",
    *,
    redis_client: Any | None = None,
) -> dict[str, Any] | None:
    symbol_u = symbol.strip().upper()
    if redis_client is not None:
        try:
            raw = redis_client.client.get(f"{REDIS_KEY}:{symbol_u}") or redis_client.client.get(REDIS_KEY)
            if raw:
                data = json.loads(raw)
                if str(data.get("symbol", "")).upper() in {"", symbol_u}:
                    return data
        except Exception:
            logger.debug("Redis strategy load failed", exc_info=True)
    path = OUT_DIR / f"top_strategies_{symbol_u.lower()}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    if SNAPSHOT_PATH.is_file():
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        if str(data.get("symbol", "")).upper() == symbol_u:
            return data
    return None


def refresh_top_strategies(
    symbol: str = "NIFTY",
    *,
    redis_client: Any | None = None,
    top_n: int = 5,
) -> dict[str, Any]:
    signals = None
    try:
        from dashboard.components.broker_speculation import load_speculation

        spec = load_speculation(redis_client)
        if spec:
            signals = dict(spec.signals)
    except Exception:
        pass
    payload = run_day_strategies(symbol, top_n=top_n, signals=signals)
    persist_strategies(payload, redis_client=redis_client)

    # Sync into Insights strategy snapshot (top strategy as headline)
    try:
        from dashboard.components.agent_journal import append_insight

        top = (payload.get("strategies") or [None])[0]
        if top:
            lines = []
            for s in payload.get("strategies") or []:
                lines.append(
                    f"#{s['rank']} {s['name']} · conf {s['confidence']:.0%} · "
                    f"win {s['win_rate']:.0%} (n={s['trades']}) · {s['structure']}"
                )
            append_insight(
                {
                    "trade_date": datetime.utcnow().date().isoformat(),
                    "symbol": symbol.strip().upper(),
                    "title": f"Top {len(payload.get('strategies') or [])} day strategies (backtested)",
                    "outlook": (
                        f"Best setup: {top['name']} (confidence {top['confidence']:.0%}). "
                        + payload.get("disclaimer", "")
                    ),
                    "strategy_for_tomorrow": " | ".join(lines),
                    "why": top.get("notes") or top.get("thesis") or "",
                    "supporting_metrics": {
                        "top_strategies": payload.get("strategies"),
                        "last_close": payload.get("last_close"),
                        "vol20": payload.get("vol20"),
                        "trend_up": payload.get("trend_up"),
                        "signals_used": payload.get("signals_used"),
                        "history_bars": payload.get("history_bars"),
                    },
                    "agent": "researcher",
                },
                redis_client=redis_client,
            )
    except Exception:
        logger.debug("Insight sync skipped", exc_info=True)
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = refresh_top_strategies("NIFTY")
    print(json.dumps({k: out[k] for k in ("symbol", "last_close", "strategies")}, indent=2)[:2500])
