# F&O Trading Data Engine — Architecture

This document describes the production architecture of the NSE Futures & Options (F&O) data engine: how live market data enters the system, how it is fanned out over ZeroMQ, how hot and warm storage are maintained, how DhanHQ / Zerodha are integrated, and which risk / operational limits gate the stack.

## 1. Goals

| Goal | Approach |
|------|----------|
| Sub-second option-chain views | Redis hot store (`ChainCache`) |
| Durable time-series for analytics / backtests | TimescaleDB warm store + daily Parquet export |
| Decouple ingest from consumers | ZeroMQ PUB/SUB fan-out |
| Dual-broker resilience | DhanHQ **or** Zerodha WebSocket streamer |
| Safe launch / stale-data awareness | NTP clock gate + tick-latency alerts |

## 2. High-level data path

```text
┌─────────────────────┐
│  Premarket (08:30)  │  cron_premarket: masters · Redis flush · NTP · docker
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  launch_trading_day │  preflight (5 pillars) → masters → Redis flush → PM2 → UI
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐     ATM ± N tokens
│  Symbol / Strike    │◄──── SymbolMapper + strike_selector
│  universe           │
└──────────┬──────────┘
           │ subscribe
           ▼
┌─────────────────────┐
│  tick_worker (PM2)  │  TickListener → ZMQ + latency + Redis + DB writer
└──────────┬──────────┘
           │ PUB  tcp://127.0.0.1:5555  topic=tick
           ▼
     ┌─────┴──────────────────────────────┐
     │         ZeroMQ fan-out             │
     └─────┬──────────┬─────────┬─────────┘
           │          │         │
           ▼          ▼         ▼
   ┌───────────┐ ┌────────┐ ┌────────────────┐
   │ Redis /   │ │ DB     │ │ greeks_engine  │
   │ ChainCache│ │ Writer │ │ (PM2) enrich   │
   └───────────┘ └────────┘ └───────┬────────┘
                                    │
           ┌────────────────────────┼────────────────────────┐
           ▼                        ▼                        ▼
   ┌───────────────┐      ┌─────────────────┐      ┌──────────────────┐
   │ auto_squareoff│      │ Streamlit       │      │ Parquet exporter │
   │ circuit break │      │ dashboard (PM2) │      │ cold path        │
   └───────────────┘      └─────────────────┘      └──────────────────┘
           │
           ▼
   ┌────────────────────────────┐     every ~2 min (no orders)
   │ strategic_controller       │──► Redis `agent:strategy:directive`
   │ (LangGraph)                │     regime · sentiment · risk limits
   └────────────────────────────┘
           │
           ▼
   ┌────────────────────────────┐     ~1s poll — orders & stops only
   │ tactical_executor          │◄── reads directive; `order_guard` LIMIT
   │ (deterministic Python)     │
   └────────────────────────────┘
```

Production supervision: [`ecosystem.config.js`](../ecosystem.config.js) (PM2, autorestart). Dev / monolithic: [`main.py`](../main.py) (spawn start method; consumers before streamer).

## 3. Redis Hot Store

**Role:** ultra-low-latency “current truth” for LTP / volume / OI on the active NIFTY and BANKNIFTY option chain.

| Piece | Location | Notes |
|-------|----------|--------|
| Client pool | [`database/redis_client.py`](../database/redis_client.py) | Connection pool; host/port from `REDIS_URL` or `REDIS_HOST` / `REDIS_PORT` |
| Chain JSON | [`database/chain_cache.py`](../database/chain_cache.py) | In-process mirror + Redis persistence |
| Premarket flush | [`cron_premarket.sh`](../cron_premarket.sh) | Deletes `tick:*` and `option_chain:*` before open |

### Key layout

| Key | Value | Purpose |
|-----|--------|---------|
| `tick:{token}` | JSON tick dict | Latest raw tick per security ID |
| `option_chain:{SYMBOL}` | JSON chain snapshot | Strike ladder with CE/PE `ltp`, `volume`, `oi`, `token` |

### Chain lifecycle

1. **Bootstrap** from `ActiveStrikeTokens` (ATM ± `num_strikes`, default 10; step e.g. 50 for NIFTY).
2. **On each tick** (`ChainCache.on_tick`): update matching CE/PE node and rewrite the symbol blob.
3. **Read path**: `get_chain("NIFTY")` / `get_all_chains()` for strategies and dashboards.

Hot store is intentionally lossy across days — flushed every premarket run so stale OI/LTP cannot poison the next session.

## 4. TimescaleDB Warm Store

**Role:** append-only warm path for historical ticks and greeks fields used by research and backtests.

| Piece | Location |
|-------|----------|
| Schema | [`database/schema.sql`](../database/schema.sql) |
| Writer | [`database/db_writer.py`](../database/db_writer.py) |
| Infra | [`docker-compose.yml`](../docker-compose.yml) (`timescaledb` on `:5432`) |
| Cold export | [`services/parquet_exporter.py`](../services/parquet_exporter.py) |
| Multi-year index OHLC | [`services/history_downloader.py`](../services/history_downloader.py) → `data/history/*_daily.parquet` (Dhan `/charts/historical`) |

### Table: `fno_ticks`

Hypertable on `time`, indexed `(token, time DESC)`:

| Column | Type | Meaning |
|--------|------|---------|
| `time` | `TIMESTAMPTZ` | Event / exchange time |
| `token` | `BIGINT` | Broker security / instrument ID |
| `last_price` | `DOUBLE` | LTP |
| `volume` | `BIGINT` | Traded volume |
| `oi` | `BIGINT` | Open interest |
| `iv` | `DOUBLE` | Implied volatility (when enriched) |
| `delta` | `DOUBLE` | Delta (when enriched) |

### Write batching (disk protection)

`DbWriter` buffers rows and flushes when **either**:

- buffer size ≥ **100** ticks, or  
- **1 second** has elapsed since the last flush  

whichever comes first. Failed flushes re-queue the batch to avoid silent data loss on transient DB outages.

### Parquet cold path

Daily IST partitions for DuckDB / offline backtests:

```text
data/YYYY-MM-DD/nifty.parquet
data/YYYY-MM-DD/banknifty.parquet
```

Compressed with PyArrow (`zstd` by default).

## 5. ZeroMQ Pub/Sub pipeline

| Piece | Location | Role |
|-------|----------|------|
| PUB | [`ingestion/zmq_pub.py`](../ingestion/zmq_pub.py) | Binds `tcp://127.0.0.1:5555`, multipart `[b"tick", json]` |
| SUB helper | [`ingestion/zmq_sub.py`](../ingestion/zmq_sub.py) | Connects (does not bind); many workers share one streamer |
| Producer | [`ingestion/tick_listener.py`](../ingestion/tick_listener.py) | WS callback → bounded queue → publisher thread |

### Design rules

1. **Only the streamer binds**; all other processes `CONNECT` as SUB.
2. WebSocket callbacks never block on ZMQ — `put_nowait` into a queue of size **50 000**; drops are logged.
3. Consumers (latency worker, Redis manager, DB writer) are independent — a slow DB cannot stall Redis updates.
4. Default endpoint override: `--zmq-endpoint` / `TRADE_ZMQ_ENDPOINT`.

## 6. DhanHQ / Zerodha API integrations

### Instrument masters

[`services/master_downloader.py`](../services/master_downloader.py) pulls daily CSVs and keeps **NSE F&O only**:

| Broker | Source | Filter |
|--------|--------|--------|
| Zerodha | `https://api.kite.trade/instruments` | `exchange == NFO` |
| Dhan | `https://images.dhan.co/api-data/api-scrip-master.csv` | `NSE` + `FUTIDX/FUTSTK/OPTIDX/OPTSTK` |

Outputs: `data/zerodha_nse_fno.csv`, `data/dhan_nse_fno.csv`.

### Symbol resolution

[`services/symbol_mapper.py`](../services/symbol_mapper.py) indexes `(symbol, expiry, strike, CE|PE) → token` for O(1) lookups used by strike selection and subscriptions.

### Live market data

[`ingestion/tick_listener.py`](../ingestion/tick_listener.py):

| Broker | Client | Reconnect | Subscribe |
|--------|--------|-----------|-----------|
| Zerodha | `kiteconnect.KiteTicker` | Built-in (`reconnect=True`), resubscribe on connect | Instrument tokens, `MODE_FULL` by default |
| Dhan | `dhanhq.MarketFeed` | Reconnect loop in feed run | `(NSE_FNO, security_id, Full)` tuples |

Auth / session tests: [`tests/test_auth.py`](../tests/test_auth.py) (Zerodha headless TOTP via `pyotp`; Dhan access-token profile validation). Secrets live in `.env` (see `.env.example`); never commit `.env` / `.env.production`.

### Analytics services (on top of ticks)

| Service | Module | Function |
|---------|--------|----------|
| Greeks | [`services/greeks_engine.py`](../services/greeks_engine.py) | IV, Δ, Θ, Γ, Vega (Black–Scholes, `r=10%`) |
| OI / PCR | [`services/oi_tracker.py`](../services/oi_tracker.py) | Call vs Put OI → PCR; Long/Short Buildup / Covering / Unwinding |
| Strikes | [`services/strike_selector.py`](../services/strike_selector.py) | ATM ± N CE/PE token set for subscription |

## 7. Risk management limits

The engine currently enforces **operational and market-data risk gates** (stale clock, delayed ticks, subscription blast radius, and OI context). Position/order hard-limits should be layered in the execution service using the same thresholds as policy inputs.

### 7.1 Launch & clock integrity

| Limit | Default | Enforcement |
|-------|---------|-------------|
| Max NTP clock drift | **50 ms** | [`services/clock_sync.py`](../services/clock_sync.py); [`services/preflight_check.py`](../services/preflight_check.py) pillar 5; [`main.py`](../main.py) / `tick_worker` abort unless skip |
| Premarket NTP check | **100 ms** | [`cron_premarket.sh`](../cron_premarket.sh) aborts before docker restart if offset too large |
| Reference clock | `time.google.com` | Logged in IST (`Asia/Kolkata`) |
| Max daily loss | **₹5000** | [`services/circuit_breaker.py`](../services/circuit_breaker.py) / PM2 `auto_squareoff`; env `MAX_DAILY_LOSS` |

Rationale: option pricing, TTE, and latency SLAs are meaningless if the host clock is wrong relative to exchange time.

### 7.2 Tick freshness

| Limit | Default | Enforcement |
|-------|---------|-------------|
| Max tick delay | **500 ms** | [`services/health_check.py`](../services/health_check.py) — `receipt_time − exchange_timestamp`; console `TICK_LATENCY_ALERT` |
| Override | `TRADE_LATENCY_THRESHOLD_MS` / `--latency-threshold-ms` | ZMQ worker process |

Delayed ticks must not drive hedge or entry decisions without an explicit stale-data policy.

### 7.3 Subscription / chain risk window

| Limit | Default | Purpose |
|-------|---------|---------|
| Active strikes | ATM ± **10** | Caps WebSocket subscription count and Redis chain size |
| NIFTY step | **50** | Strike grid alignment |
| Underlyings in hot cache | **NIFTY**, **BANKNIFTY** | Prevents unbounded chain growth |

Wider windows increase message rate, Redis write amplification, and DB insert volume — treat expansions as a capacity change.

### 7.4 Storage back-pressure

| Limit | Default | Purpose |
|-------|---------|---------|
| ZMQ publish queue | **50 000** | Protect WS thread; drop + warn when saturated |
| DB batch size | **100** rows | Avoid tiny commits thrashing disk |
| DB flush interval | **1 s** | Bound warm-store lag under low traffic |

### 7.5 Pricing / OI risk signals

| Signal | Module | Use in risk |
|--------|--------|-------------|
| IV / Greeks | `greeks_engine` | Spot-check mispriced quotes; delta exposure inputs |
| PCR (Put OI / Call OI) | `oi_tracker` | Crowding / sentiment filter for the active expiry |
| Buildup taxonomy | `oi_tracker` | Long Buildup, Short Buildup, Short Covering, Long Unwinding per strike |

These are **decision inputs**, not automatic order blocks, until wired into an execution risk service.

### 7.6 Execution / terminal risk

| Control | Default | Location |
|---------|---------|----------|
| Circuit breaker | trip at daily P&L ≤ −`MAX_DAILY_LOSS` | PM2 `auto_squareoff` → [`workers/auto_squareoff.py`](../workers/auto_squareoff.py) |
| Protected LIMIT slip | ₹0.50 vs LTP | [`services/order_guard.py`](../services/order_guard.py) |
| Emergency square-off | flatten + lock to EOD IST | [`dashboard/components/risk_controls.py`](../dashboard/components/risk_controls.py); Redis `terminal:controls` / `risk:broadcast` |

### 7.7 Security hygiene

| Control | Location |
|---------|----------|
| Secret scan on Python sources | [`tests/test_security.py`](../tests/test_security.py) |
| Env templates only in git | `.env.example`; `.env` / `.env.production` gitignored |

## 8. Runtime topology

### Preflight (five pillars)

[`services/preflight_check.py`](../services/preflight_check.py) prints a GREEN/RED checklist before launch:

1. Broker REST connected  
2. WebSocket tick feed smoke  
3. Redis reachable  
4. TimescaleDB writable  
5. NTP drift &lt; **50 ms**  

Orchestrated by [`launch_trading_day.sh`](../launch_trading_day.sh).

### PM2 cluster (`ecosystem.config.js`)

| App | Entry | Role |
|-----|--------|------|
| `tick_worker` | `workers/tick_worker.py` | WS + ZMQ latency + Redis + DB (no breaker) |
| `greeks_engine` | `workers/greeks_engine.py` | SUB → IV/greeks → Redis `tick:{token}` |
| `auto_squareoff` | `workers/auto_squareoff.py` | Circuit breaker / flatten / lock |
| `streamlit_dashboard` | `workers/streamlit_dashboard.py` | Terminal on `:8501` |

All apps set `autorestart: true` (restart on crash).

### Dev processes (`python main.py --tokens …`)

1. **websocket-streamer** — broker WS → ZMQ PUB  
2. **zmq-worker** — SUB → latency health  
3. **redis-cache** — SUB → `ChainCache`  
4. **db-writer** — SUB → batched `fno_ticks` inserts  
5. **circuit-breaker** — daily-loss monitor  

Pre-launch: `ensure_clock_synced(max_drift_ms=50)` unless skipped.

### Containers (`docker compose`)

| Service | Port | Role |
|---------|------|------|
| `redis` | 6379 | Hot store |
| `timescaledb` | 5432 | Warm store |

### Daily ops

1. `cron_premarket.sh` @ **08:30 IST** — masters → Redis flush → NTP (100 ms) → compose restart  
2. `./launch_trading_day.sh` near open — preflight (50 ms) → masters → Redis flush → PM2 → open dashboard  

Handoff summary: [AGENT_HANDOFF.md](./AGENT_HANDOFF.md).

## 9. Repository map

```text
config/               Settings (.env via pydantic-settings)
ingestion/            TickListener, zmq_pub / zmq_sub
database/             Redis, ChainCache, DbWriter, schema.sql
services/             Masters, mapper, greeks, OI, health, clock, preflight, breaker, …
workers/              PM2 entrypoints
dashboard/            Streamlit terminal + auth + Settings (local secrets)
tests/                Auth, security, mock execution
docs/                 Architecture, preflight, agent handoff
main.py               Multiprocess supervisor (dev)
ecosystem.config.js   PM2 cluster
launch_trading_day.sh Master trading-day deploy
cron_premarket.sh
docker-compose.yml
.secrets.env          Desk API keys from Settings UI (gitignored, 0600)
```

## 10. Extension points

- Wire greeks enrichment meta (strike/expiry/spot) onto every tick so PM2 `greeks_engine` always populates IV/Δ.
- Harden execution risk so latency alerts / PCR extremes hard-block `order_guard`.
- Multi-host ZMQ (TCP bind on a reachable interface) when splitting streamer and consumers across machines.
