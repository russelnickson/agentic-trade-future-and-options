#!/usr/bin/env bash
# cron_premarket.sh — daily pre-market prep at 08:30 IST
#
# Crontab (run as the deploy user):
#   CRON_TZ=Asia/Kolkata
#   30 8 * * 1-5 /Users/russelnickson/Code/trade/cron_premarket.sh >> /Users/russelnickson/Code/trade/logs/premarket.log 2>&1
#
# Steps:
#   1) Pull latest Zerodha/Dhan NSE F&O master CSVs
#   2) Flush yesterday's Redis hot cache (ticks + option chains)
#   3) Verify clock sync via NTP
#   4) Restart docker-compose services (Redis + TimescaleDB)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

export TZ="${TZ:-Asia/Kolkata}"
mkdir -p "$ROOT/logs"

LOG_TS() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { printf '[%s] %s\n' "$(LOG_TS)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

PYTHON="${ROOT}/venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3 || true)"
fi
[[ -n "${PYTHON}" ]] || die "python3 not found (expected venv at ${ROOT}/venv)"

REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6379}"
# Max acceptable absolute NTP offset in milliseconds.
NTP_MAX_OFFSET_MS="${NTP_MAX_OFFSET_MS:-100}"

log "===== Premarket job starting ====="

# ---------------------------------------------------------------------------
# 1) Latest instrument master CSVs
# ---------------------------------------------------------------------------
log "Downloading NSE F&O master CSVs..."
"$PYTHON" "${ROOT}/services/master_downloader.py" \
  || die "master_downloader.py failed"
log "Master CSV download complete."

# ---------------------------------------------------------------------------
# 2) Flush yesterday's hot Redis cache
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
# 3) Verify clock sync via NTP
# ---------------------------------------------------------------------------
log "Checking NTP clock sync (max offset ${NTP_MAX_OFFSET_MS}ms)..."

offset_within_limit() {
  local offset_sec="$1"
  local offset_ms
  offset_ms="$(awk -v s="$offset_sec" 'BEGIN {
    v = s + 0
    if (v < 0) v = -v
    printf "%.3f", v * 1000
  }')"
  log "Measured NTP offset: ${offset_ms}ms"
  awk -v ms="$offset_ms" -v max="$NTP_MAX_OFFSET_MS" 'BEGIN { exit !(ms <= max + 0) }'
}

check_ntp() {
  if command -v chronyc >/dev/null 2>&1; then
    local tracking offset_sec
    tracking="$(chronyc tracking 2>/dev/null || true)"
    [[ -n "$tracking" ]] || return 1
    echo "$tracking"
    offset_sec="$(echo "$tracking" | sed -n 's/.*Last offset[[:space:]]*:[[:space:]]*\([+-]\{0,1\}[0-9.][0-9]*\) seconds.*/\1/p')"
    [[ -n "$offset_sec" ]] || return 1
    offset_within_limit "$offset_sec"
    return $?
  fi

  if command -v timedatectl >/dev/null 2>&1; then
    timedatectl status || true
    local synced
    synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
    [[ "$synced" == "yes" ]]
    return $?
  fi

  if command -v sntp >/dev/null 2>&1; then
    # macOS: query only (no step) so cron does not need root.
    local out offset
    out="$(sntp time.google.com 2>&1 || true)"
    echo "$out"
    offset="$(echo "$out" | awk '{
      for (i = 1; i <= NF; i++) {
        if ($i ~ /^[+-][0-9]*\.[0-9]+$/) { print $i; exit }
      }
    }')"
    [[ -n "$offset" ]] || return 1
    offset_within_limit "$offset"
    return $?
  fi

  if command -v ntpdate >/dev/null 2>&1; then
    local out offset_sec
    out="$(ntpdate -q time.google.com 2>&1 || true)"
    echo "$out"
    offset_sec="$(echo "$out" | awk '/offset/ {
      for (i = 1; i <= NF; i++) {
        if ($i == "offset") { print $(i + 1); exit }
      }
    }')"
    [[ -n "$offset_sec" ]] || return 1
    offset_within_limit "$offset_sec"
    return $?
  fi

  return 2
}

if check_ntp; then
  log "NTP sync OK."
else
  rc=$?
  if [[ "$rc" -eq 2 ]]; then
    die "No NTP client found (install chrony, timedatectl, sntp, or ntpdate)."
  fi
  die "Clock not synchronized within ${NTP_MAX_OFFSET_MS}ms — aborting before market open."
fi

# ---------------------------------------------------------------------------
# 4) Restart docker containers
# ---------------------------------------------------------------------------
log "Restarting docker compose services..."
if ! command -v docker >/dev/null 2>&1; then
  die "docker not found on PATH"
fi

if docker compose version >/dev/null 2>&1; then
  docker compose -f "${ROOT}/docker-compose.yml" restart \
    || die "docker compose restart failed"
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose -f "${ROOT}/docker-compose.yml" restart \
    || die "docker-compose restart failed"
else
  die "docker compose plugin / docker-compose not available"
fi

log "Docker services restarted."
log "===== Premarket job finished successfully ====="
