#!/usr/bin/env bash
# TradeBridge watchdog — runs every 5 min via systemd timer.
# SAFE actions only: restart a wedged bot, record anomalies.
# It NEVER edits code, deploys, or touches trading state.
#
# Hardening 2026-10-23 (858 restarts in a week): the old script restarted on
# a single /api/status miss. /api/status aggregates broker/feed/DB state and
# is exactly what stalls when the event loop is busy — so every restorm fed
# itself. Now: probe a cheap /api/health, require 2 consecutive failures,
# and cap restarts at 4/hour. A wedged desk is left alone for a human.
APP_DIR=/home/ubuntu/kotaktegramv2
LOG=$APP_DIR/data/watchdog.log
STATUS=$APP_DIR/data/watchdog-status.json
FAILS=$APP_DIR/data/watchdog-fails
RESTARTS=$APP_DIR/data/watchdog-restarts
BOT_URL=http://127.0.0.1:8001/api/health
MAX_FAILS=2            # consecutive failed probes before a restart
MAX_RESTARTS_HOUR=4    # never restart-loop the desk
ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
note() { echo "$(ts) $1" >> "$LOG"; }
STATUS_TXT=online
ACTIONS=""

# Restarts used in the last hour (also prunes old entries).
restarts_last_hour() {
  local now cutoff n t
  now=$(date +%s); cutoff=$((now - 3600)); n=0
  [ -f "$RESTARTS" ] || { echo 0; return; }
  : > "$RESTARTS.tmp"
  while read -r t; do
    [ -z "$t" ] && continue
    case "$t" in *[!0-9]*) continue;; esac
    if [ "$t" -gt "$cutoff" ]; then
      echo "$t" >> "$RESTARTS.tmp"; n=$((n + 1))
    fi
  done < "$RESTARTS"
  mv "$RESTARTS.tmp" "$RESTARTS"
  echo "$n"
}

do_restart() {
  local reason=$1
  local recent
  recent=$(restarts_last_hour)
  if [ "$recent" -ge "$MAX_RESTARTS_HOUR" ]; then
    note "WATCHDOG: $reason but restart budget spent ($recent/h) — leaving process alone"
    ACTIONS="${ACTIONS}no-restart-budget "
    STATUS_TXT=attention
    return 1
  fi
  note "WATCHDOG: $reason — restarting"
  pm2 restart kotak-trader >> "$LOG" 2>&1
  date +%s >> "$RESTARTS"
  echo 0 > "$FAILS"
  ACTIONS="${ACTIONS}restart "
  STATUS_TXT=restarted
  sleep 20
  return 0
}

# 1. PM2 process alive?
if ! pm2 list 2>/dev/null | grep -q 'kotak-trader.*online'; then
  do_restart "kotak-trader not online"
fi

# 2. Health probe (cheap, in-memory). Two strikes before acting.
if curl -sf --max-time 10 "$BOT_URL" -o /dev/null 2>/dev/null; then
  echo 0 > "$FAILS"
else
  fails=$(cat "$FAILS" 2>/dev/null || echo 0)
  case "$fails" in *[!0-9]*) fails=0;; esac
  fails=$((fails + 1))
  echo "$fails" > "$FAILS"
  if [ "$fails" -ge "$MAX_FAILS" ]; then
    do_restart "/api/health unresponsive ${fails}x"
  else
    note "WATCHDOG: /api/health probe failed (${fails}/${MAX_FAILS}) — waiting"
    ACTIONS="${ACTIONS}probe-fail "
    STATUS_TXT=attention
  fi
fi

# 3. Recent error burst? (last ~500 lines, alert threshold 20). Alert only.
ERRS=$(pm2 logs kotak-trader --lines 500 --nostream 2>/dev/null | grep -ciE 'ERROR|Traceback' || true)
if [ "$ERRS" -gt 20 ]; then
  note "WATCHDOG: error burst ($ERRS/500 lines) — needs human review"
  ACTIONS="${ACTIONS}error-burst "
  STATUS_TXT=attention
fi

# 4. Disk + DB writable?
DISK=$(df / | tail -1 | awk '{print $5}' | tr -d '%')
if [ "$DISK" -gt 85 ]; then
  note "WATCHDOG: disk ${DISK}%"
  ACTIONS="${ACTIONS}disk-high "
fi
if ! python3 -c "import sqlite3; sqlite3.connect('$APP_DIR/data/trades.db').execute('SELECT 1').fetchone()" 2>/dev/null; then
  note 'WATCHDOG: trades.db unreadable'
  ACTIONS="${ACTIONS}db-bad "
  STATUS_TXT=attention
fi

# 5. Record a machine-readable summary.
cat > "$STATUS" <<EOF
{"checked_at":"$(ts)","status":"$STATUS_TXT","actions":"$(echo "$ACTIONS" | sed 's/ *$//')","recent_errors":${ERRS:-0},"restarts_last_hour":$(restarts_last_hour)}
EOF
