# F&O Trading Data Engine

Real-time NSE Futures & Options data stack: dual-broker WebSocket ingest (Zerodha / DhanHQ), ZeroMQ fan-out, Redis hot option-chain cache, TimescaleDB warm ticks, Greeks / OI analytics, risk circuit breaker, and a Streamlit **Console** — a multi-agent live desk that aims to close each session on a decent trade day.

| Doc | Purpose |
|-----|---------|
| [docs/AGENT_HANDOFF.md](docs/AGENT_HANDOFF.md) | **Start here for agent handoff** — launch path, PM2, limits, Redis keys |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, stores, ZMQ, risk limits |
| [docs/PREFLIGHT_CHECKLIST.md](docs/PREFLIGHT_CHECKLIST.md) | 08:30 IST SOP + five-pillar readiness |

---

## Quickstart installation

### Requirements

- Python **3.11+** (3.12/3.14 OK)
- Docker + Docker Compose
- Node.js **PM2** (`npm install -g pm2`) for the trading-day cluster
- macOS / Linux (IST ops assumed)

### 1. Clone and create a virtualenv

```bash
cd trade
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in at least:

| Variable | Purpose |
|----------|---------|
| `DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` | DhanHQ session |
| `ZERODHA_API_KEY` / `ZERODHA_API_SECRET` / `ZERODHA_USER_ID` / `ZERODHA_PASSWORD` / `ZERODHA_TOTP_SECRET` | Headless Kite login |
| `ZERODHA_ACCESS_TOKEN` | Optional daily session token for WS/REST |
| `REDIS_URL` | e.g. `redis://localhost:6379/0` |
| `DATABASE_URL` | e.g. `postgresql://trade:trade@localhost:5432/trade` |
| `TRADE_TOKENS` | Comma-separated F&O tokens (required for PM2 `tick_worker`) |
| `TRADE_BROKER` | `dhan` or `zerodha` |
| `MAX_DAILY_LOSS` | Circuit breaker threshold (default ₹5000) |

`.env` and `.env.production` are gitignored — never commit secrets.

### 3. Apply the TimescaleDB schema (after Docker is up)

```bash
psql "$DATABASE_URL" -f database/schema.sql
```

---

## Running Docker containers

Start **Redis** (hot store) and **TimescaleDB** (warm store):

```bash
docker compose up -d
docker compose ps
```

| Service | Port | Role |
|---------|------|------|
| `redis` | `6379` | Latest ticks + option-chain JSON |
| `timescaledb` | `5432` | `fno_ticks` hypertable |

Default DB URL matching compose:

```text
postgresql://trade:trade@localhost:5432/trade
```

---

## Trading-day master launch (preferred)

One script runs preflight → masters → Redis flush → PM2 → browser:

```bash
source venv/bin/activate
export TRADE_TOKENS="123456,789012"
export TRADE_BROKER=dhan   # or zerodha
./launch_trading_day.sh
```

### What it does

1. **`python services/preflight_check.py`** — five pillars, GREEN/RED checklist; aborts on RED  
2. **Download** daily NSE F&O master CSVs  
3. **Flush** Redis `tick:*` / `option_chain:*`  
4. **`pm2 start|reload ecosystem.config.js`** — crash-autorestart cluster  
5. **Open** Streamlit at [http://localhost:8501](http://localhost:8501)

| Escape hatch | Effect |
|--------------|--------|
| `SKIP_PREFLIGHT=1` | Skip pillar checks (ops only) |
| `SKIP_BROWSER=1` | Do not open the browser |
| `DASHBOARD_URL=…` | Override dashboard URL |

### Five preflight pillars

| # | Pillar | Pass |
|---|--------|------|
| 1 | Broker REST API connected | Profile call OK |
| 2 | WebSocket tick feed active | Short `TickListener` smoke |
| 3 | Redis hot memory reachable | PING + write probe |
| 4 | TimescaleDB writable | INSERT/DELETE probe |
| 5 | Clock NTP drift &lt; 50 ms | vs `time.google.com` |

Standalone:

```bash
python services/preflight_check.py
```

---

## PM2 process cluster

Config: [`ecosystem.config.js`](ecosystem.config.js) (interpreter: `venv/bin/python`, `autorestart: true`).

| App | Script | Responsibility |
|-----|--------|----------------|
| `tick_worker` | `workers/tick_worker.py` | WS → ZMQ + latency worker + Redis cache + DB writer |
| `greeks_engine` | `workers/greeks_engine.py` | ZMQ SUB → IV/greeks → Redis latest tick |
| `auto_squareoff` | `workers/auto_squareoff.py` | Daily-loss circuit breaker / emergency flatten |
| `streamlit_dashboard` | `workers/streamlit_dashboard.py` | Streamlit terminal on `:8501` |

```bash
pm2 start ecosystem.config.js
pm2 status
pm2 logs
pm2 restart all
pm2 stop all
```

---

## Dev supervisor (without PM2)

`main.py` still launches a monolithic multiprocess stack (includes circuit-breaker child):

1. WebSocket streamer → ZeroMQ PUB (`tcp://127.0.0.1:5555`)  
2. ZMQ worker — latency alerts if delay &gt; **500 ms**  
3. Redis cache manager  
4. DB writer (batch 100 rows **or** 1 s)  
5. Circuit breaker  

```bash
export TRADE_TOKENS="..."
python main.py --tokens "$TRADE_TOKENS" --broker "$TRADE_BROKER" \
  --latency-threshold-ms 500 --max-clock-drift-ms 50
```

Pre-launch NTP gate: abort if drift &gt; **50 ms** unless `--skip-clock-sync`.

---

## Pre-market automation (08:30 IST)

Full SOP: [docs/PREFLIGHT_CHECKLIST.md](docs/PREFLIGHT_CHECKLIST.md).

```bash
./cron_premarket.sh
# CRON_TZ=Asia/Kolkata
# 30 8 * * 1-5 /path/to/trade/cron_premarket.sh >> /path/to/trade/logs/premarket.log 2>&1
```

1. Download master CSVs  
2. Clear Redis hot keys  
3. NTP check (script limit **100 ms**)  
4. Restart Docker Compose  

Then prefer `./launch_trading_day.sh` closer to open (stricter 50 ms preflight + PM2).

---

## Streamlit terminal

```bash
# EC2 worker telemetry (health · positions · emergency square-off)
streamlit run dashboard/app.py

# Full multi-agent Trade Console (preferred with launch / PM2)
streamlit run dashboard/Trade_Console.py
```

**Sign-in:** username `russelnickson` (local session; see `dashboard/auth.py`). Sidebar shows profile **Russel Nickson** + placeholder avatar.

**Settings** (sidebar page): configure broker + API keys with help popovers and **Validate Dhan / Zerodha / Redis** on the same screen. Secrets write only to gitignored `.secrets.env` (mode 0600) — never commit that file.

| Control | Behavior |
|---------|----------|
| Kill switch / square-off | Redis `terminal:controls` + `risk:broadcast` |
| Emergency square-off | Flatten + lock trading until EOD IST |
| Order audit | Redis stream `orders:audit` / `logs/execution_orders.jsonl` |
| Agents | Multi-agent discussion + Trade decisions (`agent:conversations` / `agent:decisions`) |
| Insights | Top 5 backtested day strategies + confidence (`services/day_strategy_backtest.py`) |
| Global Outlook | Overseas / GIFT / MCX markers + FII·DII + composite open bias (`services/global_outlook.py`) |
| Live Market | Direct-source voices (RBI/PIB/NITI/NSE Nifty 100 + Fed/ECB/BoE/BIS) with credibility tiers |
| Settings | Broker credentials → local `.secrets.env` only |

**Trade Console** (home): **F&O Trading Console & Agentic Dashboard** — live desk during market hours — Scout · Voices · Research · Risk · **Trade** debate facts and aim to close a **decent** day (phenomenal / okay / flat / acceptable loss).


---

## Project layout

```text
config/                 pydantic-settings (.env)
ingestion/              TickListener, zmq_pub / zmq_sub
database/               Redis, ChainCache, DbWriter, schema.sql
services/               masters, mapper, greeks, OI, clock, preflight, circuit breaker, …
workers/                PM2 entrypoints (tick, greeks, auto_squareoff, streamlit)
dashboard/              Streamlit terminal
tests/                  auth, security, mock execution
docs/                   architecture, preflight SOP, agent handoff
main.py                 multiprocess supervisor (dev)
ecosystem.config.js     PM2 cluster
launch_trading_day.sh   master trading-day deploy
cron_premarket.sh       08:30 IST prep
docker-compose.yml
```

---

## Common commands cheat sheet

```bash
docker compose up -d
psql "$DATABASE_URL" -f database/schema.sql

python services/preflight_check.py
./launch_trading_day.sh
pm2 status && pm2 logs

./cron_premarket.sh
python services/clock_sync.py
python main.py --tokens "$TRADE_TOKENS" --broker zerodha

python -m unittest tests.test_auth tests.test_security tests.test_mock_execution -v
streamlit run dashboard/app.py
streamlit run dashboard/Trade_Console.py
python services/parquet_exporter.py --date YYYY-MM-DD --symbol NIFTY
```

---

## License / disclaimer

This stack is for research and operational market-data engineering. Automating broker login may conflict with broker terms of service — use at your own risk. Not investment advice.
