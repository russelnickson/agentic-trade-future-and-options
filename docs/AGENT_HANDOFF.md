# Agent handoff — F&O Trading Data Engine

Single-page context for the next agent. Prefer this file + [ARCHITECTURE.md](./ARCHITECTURE.md) + [PREFLIGHT_CHECKLIST.md](./PREFLIGHT_CHECKLIST.md) + root [README.md](../README.md).

## What this repo is

Production-oriented NSE F&O data/execution stack under the project root:

- Dual-broker ingest (Zerodha KiteTicker / Dhan MarketFeed) → ZeroMQ PUB
- Redis hot option-chain + latest ticks; TimescaleDB warm `fno_ticks`
- Greeks / OI analytics, Streamlit terminal, circuit breaker / order guard
- Daily launch via **preflight → masters → Redis flush → PM2 → browser**

## Canonical launch path (trading day)

```bash
cd /path/to/trade
source venv/bin/activate
docker compose up -d
cp .env.example .env   # fill secrets once
export TRADE_TOKENS="..."   # required
export TRADE_BROKER=dhan    # or zerodha
./launch_trading_day.sh
```

`launch_trading_day.sh` runs, in order:

1. `python services/preflight_check.py` — **5 pillars**, GREEN/RED console; exit 1 aborts  
2. `python services/master_downloader.py` — daily NSE F&O CSVs  
3. Flush Redis `tick:*` + `option_chain:*`  
4. `pm2 start|reload ecosystem.config.js` — autorestart cluster  
5. Open `http://localhost:8501` (Streamlit)

Premarket-only (08:30 IST, no PM2): `./cron_premarket.sh`.

## PM2 apps (`ecosystem.config.js`)

| App | Entry | Role |
|-----|--------|------|
| `tick_worker` | `workers/tick_worker.py` | WS streamer + ZMQ latency + Redis cache + DB writer (no circuit breaker) |
| `greeks_engine` | `workers/greeks_engine.py` | ZMQ SUB → IV/greeks enrich → Redis `tick:{token}` |
| `auto_squareoff` | `workers/auto_squareoff.py` | Daily-loss circuit breaker → cancel/flatten/lock |
| `streamlit_dashboard` | `workers/streamlit_dashboard.py` | `streamlit run dashboard/Trade_Console.py` |

All apps: `autorestart: true`, interpreter `venv/bin/python`. Requires global `pm2` (`npm i -g pm2`).

Dev alternative without PM2: `python main.py --tokens …` (includes circuit-breaker child).

## Preflight pillars (`services/preflight_check.py`)

| # | Pillar | Pass when |
|---|--------|-----------|
| 1 | Broker REST | Profile/funds call succeeds for `TRADE_BROKER` |
| 2 | WebSocket feed | Short `TickListener` smoke (`TRADE_TOKENS` / `TRADE_PREFLIGHT_TOKENS`) |
| 3 | Redis | PING + SET/GET probe |
| 4 | TimescaleDB | INSERT/DELETE on `preflight_write_probe` |
| 5 | NTP clock | `|drift| < 50ms` vs `time.google.com` (`TRADE_MAX_CLOCK_DRIFT_MS`) |

## Hard limits (do not “fix” casually)

| Limit | Value | Module |
|-------|-------|--------|
| NTP launch drift | **50 ms** | `clock_sync`, preflight, `main` / tick_worker |
| Premarket NTP (cron script) | **100 ms** | `cron_premarket.sh` |
| Tick latency alert | **500 ms** | `health_check` / ZMQ worker |
| Max daily loss | **₹5000** | `MAX_DAILY_LOSS` / circuit breaker |
| Protected limit slip | **₹0.50** | `order_guard` |

## Redis control keys

| Key | Purpose |
|-----|---------|
| `tick:{token}` | Latest tick JSON |
| `option_chain:{SYMBOL}` | Chain ladder |
| `terminal:controls` | Dashboard kill-switch / trading lock |
| `risk:broadcast` | Emergency / risk pub |
| `risk:emergency:last` | Last emergency event |
| `risk:circuit_breaker:*` | Breaker state |
| `orders:audit` | Order audit stream (dashboard) |
| `agent:conversations` | Live agent chat / commentary turns |
| `agent:decisions` | Agent decisions + rationale |
| `agent:insights` | Historic / next-day strategy notes |
| `agent:strategy:today` | Latest tomorrow-plan snapshot |

## Important paths

```text
ecosystem.config.js      PM2 cluster
launch_trading_day.sh    Master deploy
cron_premarket.sh        08:30 IST prep
services/preflight_check.py
workers/{tick_worker,greeks_engine,auto_squareoff,streamlit_dashboard}.py
main.py                  Multiprocess supervisor (dev / monolithic)
dashboard/Trade_Console.py  Trade Console — F&O Trading Console & Agentic Dashboard
dashboard/pages/2_Agents.py  Agent discussion + Trade decisions
dashboard/components/console_runtime.py  Session clock, day grade, agent briefing
dashboard/pages/4_Global_Outlook.py  Global markers + FII/DII bias
dashboard/pages/5_Live_Market.py     Direct-source market voices
services/global_outlook.py           Dhan + Yahoo + NSE FII/DII fetcher
services/live_market_voices.py       RBI/PIB/NITI/NSE + Fed/ECB/BoE/BIS (no synthetic quotes)
docs/ARCHITECTURE.md
docs/PREFLIGHT_CHECKLIST.md
```

**Console day grades (decent close):** PHENOMENAL · OKAY · FLAT · ACCEPTABLE_LOSS (BREACH = not decent).

Agents: **Scout** (global) · **Voices** (direct sources) · **Research** (chain/PCR) · **Risk** · **Trade** (decides).

Refresh Global Outlook cache (pre-open):

```bash
venv/bin/python -c "from dashboard.secrets_store import apply_secrets_to_environ as a; a(); from services.global_outlook import refresh_global_outlook as r; s=r(); print(s.bias, s.score)"
```

Caches land in `data/global/` (`markers_latest.parquet`, `fii_dii_daily.parquet`, `outlook_snapshot.json`).

Refresh Live Market voices:

```bash
venv/bin/python -c "from services.live_market_voices import refresh_live_market as r; s=r(); print(s.counts_by_horizon)"
```

Caches land in `data/live_market/` (verbatim titles/URLs only; credibility = source-tier).

## Env / secrets

- Template: `.env.example` — copy to `.env` (gitignored)
- **Dashboard Settings** writes `/.secrets.env` (gitignored, mode **0600**) — preferred for desk API keys
- Template stub: `.secrets.env.example` (no real values)
- Never commit `.env`, `.env.production`, `.secrets.env`, tokens, or parquet/logs with PII
- Streamlit sign-in: user `russelnickson` (local session only; password digest in `dashboard/auth.py`)
- Zerodha live WS needs `ZERODHA_ACCESS_TOKEN` (or headless TOTP login fields)
- Dhan: `DHAN_CLIENT_ID` + `DHAN_ACCESS_TOKEN`

## Tests

```bash
python -m unittest tests.test_auth tests.test_security tests.test_mock_execution -v
```

## Agent do / don’t

**Do:** keep docs in sync when changing launch flow, Redis keys, or risk limits; fail closed on RED preflight; use PM2 for production-shaped runs.

**Don’t:** commit secrets; force-push; skip NTP/loss gates without an explicit ops override (`SKIP_PREFLIGHT` / `--skip-clock-sync` are escape hatches only).
