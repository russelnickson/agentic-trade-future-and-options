#!/usr/bin/env bash
# launch_trading_day.sh — master trading-day deployment
#
# Sequence:
#   1) Preflight readiness (5 pillars GREEN/RED)
#   2) Download daily NSE F&O master CSVs
#   3) Flush stale Redis hot cache (tick:* / option_chain:*)
#   4) Start PM2 process cluster (tick_worker, greeks_engine, auto_squareoff, streamlit_dashboard)
#   5) Open Streamlit dashboard in the default browser
#
# Usage:
#   export TRADE_TOKENS="123,456"   # required for tick_worker / WS preflight
#   ./launch_trading_day.sh
#
# Optional:
#   SKIP_PREFLIGHT=1   — skip pillar checks (not recommended)
#   SKIP_BROWSER=1     — do not open the dashboard URL
#   DASHBOARD_URL      — default http://localhost:8501

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

export TZ="${TZ:-Asia/Kolkata}"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
export TRADE_SKIP_CLOCK_SYNC="${TRADE_SKIP_CLOCK_SYNC:-1}"  # preflight already gated NTP

mkdir -p "$ROOT/logs"

LOG_TS() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { printf '[%s] %s\n' "$(LOG_TS)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

PYTHON="${ROOT}/venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3 || true)"
fi
[[ -n "${PYTHON}" ]] || die "python3 not found (expected venv at ${ROOT}/venv)"

# Load .env into the shell for TRADE_* / Redis host hints (secrets stay in process env).
if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6379}"
DASHBOARD_URL="${DASHBOARD_URL:-http://localhost:8501}"
ECOSYSTEM="${ROOT}/ecosystem.config.js"

log "===== Trading day launch starting ====="

# ---------------------------------------------------------------------------
# 1) Preflight — five pillars
# ---------------------------------------------------------------------------
if [[ "${SKIP_PREFLIGHT:-0}" == "1" ]]; then
  log "SKIP_PREFLIGHT=1 — skipping services/preflight_check.py"
else
  log "Running preflight checklist..."
  "$PYTHON" "${ROOT}/services/preflight_check.py" \
    || die "Preflight RED — aborting launch (fix pillars, then retry)"
  log "Preflight GREEN — continuing."
fi

# ---------------------------------------------------------------------------
# 2) Daily master CSVs
# ---------------------------------------------------------------------------
log "Downloading NSE F&O master CSVs..."
"$PYTHON" "${ROOT}/services/master_downloader.py" \
  || die "master_downloader.py failed"
log "Master CSV download complete."

# ---------------------------------------------------------------------------
# 3) Flush old Redis hot cache
# ---------------------------------------------------------------------------
log "Flushing Redis hot cache (tick:* / option_chain:*) on ${REDIS_HOST}:${REDIS_PORT}..."
if command -v redis-cli >/dev/null 2>&1; then
  deleted=0
  while IFS= read -r key; do
    [[ -z "$key" ]] && continue
    redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" DEL "$key" >/dev/null
    deleted=$((deleted + 1))
  done < <(redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" --scan --pattern 'tick:*')
  while IFS= read -r key; do
    [[ -z "$key" ]] && continue
    redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" DEL "$key" >/dev/null
    deleted=$((deleted + 1))
  done < <(redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" --scan --pattern 'option_chain:*')
  log "Deleted ${deleted} hot-cache key(s)."
else
  log "redis-cli not found; flushing via Python redis client..."
  "$PYTHON" - <<'PY'
from database.redis_client import RedisClient

client = RedisClient.from_settings().client
deleted = 0
for pattern in ("tick:*", "option_chain:*"):
    for key in client.scan_iter(match=pattern, count=500):
        deleted += int(client.delete(key))
print(f"deleted={deleted}")
client.connection_pool.disconnect()
PY
fi

# ---------------------------------------------------------------------------
# 4) PM2 process cluster
# ---------------------------------------------------------------------------
command -v pm2 >/dev/null 2>&1 || die "pm2 not found — install with: npm install -g pm2"
[[ -f "$ECOSYSTEM" ]] || die "Missing ${ECOSYSTEM}"

if [[ -z "${TRADE_TOKENS:-}" ]]; then
  die "TRADE_TOKENS is required before starting tick_worker (export comma-separated IDs)"
fi

log "Starting / restarting PM2 apps from ecosystem.config.js..."
# Prefer restart-of-existing cluster; fall back to fresh start.
if pm2 describe tick_worker >/dev/null 2>&1; then
  pm2 reload "$ECOSYSTEM" --update-env \
    || pm2 restart ecosystem.config.js --update-env \
    || die "pm2 reload/restart failed"
else
  pm2 start "$ECOSYSTEM" --update-env \
    || die "pm2 start failed"
fi
pm2 save >/dev/null 2>&1 || true
pm2 status
log "PM2 cluster up (autorestart enabled on crash)."

# ---------------------------------------------------------------------------
# 5) Open Streamlit dashboard
# ---------------------------------------------------------------------------
log "Waiting briefly for Streamlit on ${DASHBOARD_URL}..."
"$PYTHON" - <<PY || true
import time, urllib.request
url = "${DASHBOARD_URL}"
for _ in range(30):
    try:
        urllib.request.urlopen(url, timeout=1)
        break
    except Exception:
        time.sleep(0.5)
PY

if [[ "${SKIP_BROWSER:-0}" != "1" ]]; then
  log "Opening dashboard: ${DASHBOARD_URL}"
  if command -v open >/dev/null 2>&1; then
    open "${DASHBOARD_URL}" || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${DASHBOARD_URL}" >/dev/null 2>&1 || true
  else
    log "No browser launcher found — open manually: ${DASHBOARD_URL}"
  fi
else
  log "SKIP_BROWSER=1 — dashboard URL: ${DASHBOARD_URL}"
fi

log "===== Trading day launch finished ====="
log "Useful: pm2 logs | pm2 status | pm2 stop all"
