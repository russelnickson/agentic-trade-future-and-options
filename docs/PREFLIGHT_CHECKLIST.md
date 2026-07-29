# Pre-Market Checklist — 08:30 AM IST

Standard operating procedure (SOP) for preparing the F&O data engine **before NSE cash/FO open**. Target window: **08:30–09:15 IST** on trading days.

| Path | When |
|------|------|
| [`cron_premarket.sh`](../cron_premarket.sh) | 08:30 backbone (masters, Redis flush, NTP 100 ms, docker restart) |
| [`services/preflight_check.py`](../services/preflight_check.py) | Five-pillar GREEN/RED readiness (NTP **50 ms**) |
| [`launch_trading_day.sh`](../launch_trading_day.sh) | Master deploy: preflight → masters → Redis flush → PM2 → browser |

Related: [ARCHITECTURE.md](./ARCHITECTURE.md) · [AGENT_HANDOFF.md](./AGENT_HANDOFF.md)

---

## 0. Preconditions

- [ ] Host timezone / ops context is **IST** (`Asia/Kolkata`).
- [ ] `.env` (or `.env.production`) is present and **not** committed; secrets match today’s session needs.
- [ ] Project venv is available: `trade/venv/bin/python`.
- [ ] Docker is up: Redis `:6379`, TimescaleDB `:5432`.
- [ ] PM2 installed (`npm install -g pm2`) for cluster launch.
- [ ] `TRADE_TOKENS` (and `TRADE_BROKER`) exported for tick worker / WS smoke.
- [ ] You are on a trading day (NSE holiday calendar checked).

**Crontab (automated backbone):**

```cron
CRON_TZ=Asia/Kolkata
30 8 * * 1-5 /path/to/trade/cron_premarket.sh >> /path/to/trade/logs/premarket.log 2>&1
```

If cron is disabled, run manually at 08:30:

```bash
cd /path/to/trade
./cron_premarket.sh
```

---

## 1. Five-pillar automated preflight (required before PM2)

```bash
export TRADE_TOKENS="..."          # or TRADE_PREFLIGHT_TOKENS
export TRADE_BROKER=dhan           # or zerodha
python services/preflight_check.py
```

Console prints a **GREEN/RED** checklist:

| # | Pillar | GREEN when |
|---|--------|------------|
| 1 | Broker REST API connected | Profile / session call succeeds |
| 2 | WebSocket tick feed active | Short `TickListener` smoke (tick or stable session) |
| 3 | Redis hot memory reachable | PING + write probe |
| 4 | TimescaleDB writable | INSERT/DELETE on `preflight_write_probe` |
| 5 | Clock NTP drift &lt; 50 ms | vs `time.google.com` |

- [ ] All five pillars **GREEN** (exit code 0).
- [ ] On any **RED**, do **not** start PM2 / place orders; fix and re-run.

`./launch_trading_day.sh` runs this automatically and aborts on RED (`SKIP_PREFLIGHT=1` is an explicit ops escape hatch only).

---

## 2. Master contract downloads

**Why:** Expiry rolls, new strikes, and lot-size changes invalidate yesterday’s token map.

### Automated (preferred)

`cron_premarket.sh` and `launch_trading_day.sh` both run:

```bash
python services/master_downloader.py
```

### Verify

- [ ] `data/zerodha_nse_fno.csv` and `data/dhan_nse_fno.csv` have **today’s** mtime (IST).
- [ ] Row counts are non-trivial; sample ATM option resolves via `SymbolMapper`.

```bash
python - <<'PY'
from services.symbol_mapper import SymbolMapper
m = SymbolMapper.from_zerodha()  # or from_dhan()
print("options indexed:", len(m), "broker=", m.broker)
PY
```

**Fail / escalate:** empty CSVs or mapper miss → do **not** launch the streamer.

---

## 3. Check capital & margins

**Why:** Human gate before any strategy that may place hedges or rolls.

### Zerodha

```bash
python -m unittest tests.test_auth.TestZerodhaHeadlessAuth -v
```

### DhanHQ

```bash
python -m unittest tests.test_auth.TestDhanAccessToken -v
```

### Checklist

- [ ] Broker session/token is **valid today**.
- [ ] Available cash / collateral meets desk minimum.
- [ ] No conflicting overnight positions / utilised margin.

---

## 4. WebSocket connection (manual smoke if needed)

Automated pillar 2 covers a short smoke. For a longer manual check:

```bash
python -m ingestion.tick_listener --broker "$TRADE_BROKER" --tokens "$TRADE_TOKENS" --mode full
```

In another terminal:

```bash
python - <<'PY'
from ingestion.zmq_sub import iter_ticks
for i, tick in enumerate(iter_ticks()):
    print("got tick", tick)
    if i >= 2:
        break
PY
```

- [ ] Connect + subscribe without auth errors.
- [ ] Stop the smoke listener before starting the full PM2 cluster (port `5555` bind conflict).

---

## 5. Clear stale Redis keys

**Why:** Yesterday’s `tick:*` / `option_chain:*` must not appear as live state.

Automated by `cron_premarket.sh` and `launch_trading_day.sh`. Manual:

```bash
redis-cli --scan --pattern 'tick:*' | while read -r k; do redis-cli DEL "$k"; done
redis-cli --scan --pattern 'option_chain:*' | while read -r k; do redis-cli DEL "$k"; done
```

- [ ] Hot-cache keys cleared; Redis `PING` → `PONG`.

Also clear / reset terminal locks if carrying over from a prior emergency:

- Redis: `terminal:controls`, `risk:circuit_breaker:*` (ops judgment).

---

## 6. Recommended full morning sequence

| Time (IST) | Step | Owner |
|------------|------|--------|
| 08:30 | `cron_premarket.sh` (masters + Redis flush + NTP 100 ms + docker) | Automation |
| 08:40 | Capital / margin check | Ops / Risk |
| 08:50 | Confirm `TRADE_TOKENS` for today’s ATM ladder | Ops |
| 08:55–09:10 | `./launch_trading_day.sh` (preflight 50 ms → PM2 → Streamlit) | Ops |
| 09:15+ | Monitor `pm2 status` / dashboard; breaker armed via `auto_squareoff` | Ops |

### PM2 apps expected UP

| App | Role |
|-----|------|
| `tick_worker` | Ingest + Redis + DB |
| `greeks_engine` | IV/greeks enrich |
| `auto_squareoff` | Daily-loss circuit breaker |
| `streamlit_dashboard` | Terminal UI |

```bash
pm2 status
pm2 logs --lines 100
```

---

## 7. Go / No-Go summary

| Gate | Go if… |
|------|--------|
| Preflight | All 5 pillars **GREEN** |
| Masters | Fresh NSE F&O CSVs; mapper resolves active strikes |
| Capital | Funds/margins API OK; available ≥ desk minimum |
| Redis | Stale hot keys cleared |
| Clock | NTP drift ≤ **50 ms** (launch) / **100 ms** (cron script) |
| PM2 | Four apps online with autorestart |

**No-Go:** any RED pillar or failed gate unless risk explicitly signs off on a degraded mode (e.g. data-only, no orders).

---

## 8. Log artifacts

- Premarket: `logs/premarket.log`
- Launch: shell stdout from `launch_trading_day.sh`
- PM2: `pm2 logs` (`tick_worker`, `greeks_engine`, `auto_squareoff`, `streamlit_dashboard`)
- Engine (dev): `main.py` child process logs
- Never paste full access tokens into ops notes
