#!/bin/bash
# Quick monitor for port 8765. Run every minute (cron). On failure: log alert, attempt restart, escalate after 3 failures.
# Usage: ./tools/monitor_8765.sh   (run from mint-ladder-bot project root or set MINT_LADDER_ROOT)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${MINT_LADDER_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
RUNTIME_ROOT="${RUNTIME_ROOT:-$(cd "$ROOT/.." && pwd)/runtime}"
MONITOR_DIR="${MONITOR_DIR:-$RUNTIME_ROOT/monitor}"
mkdir -p "$MONITOR_DIR"
cd "$ROOT"
LOG="${MONITOR_DIR}/monitor_8765.log"
FAILURE_COUNT_FILE="${MONITOR_DIR}/.monitor_8765_failures"
ESCALATION_FILE="${MONITOR_DIR}/escalation.jsonl"
MAX_FAILURES=3
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if curl -sf --connect-timeout 3 "http://127.0.0.1:8765/" >/dev/null 2>&1; then
  echo "0" > "$FAILURE_COUNT_FILE"
  exit 0
fi

# Failure: log alert
echo "[$TS] ALERT: localhost:8765 not responding" >> "$LOG"
count=0
[ -f "$FAILURE_COUNT_FILE" ] && count=$(cat "$FAILURE_COUNT_FILE")
count=$((count + 1))
echo "$count" > "$FAILURE_COUNT_FILE"

# Attempt restart: start HTTP server on 8765 if nothing listening
if ! lsof -i :8765 >/dev/null 2>&1; then
  (cd "$ROOT" && nohup python3 -m http.server 8765 >> "${ROOT}/monitor_8765_server.log" 2>&1 &)
  echo "[$TS] Restart attempted: python3 -m http.server 8765" >> "$LOG"
  sleep 2
fi

# Escalate after repeated failures
if [ "$count" -ge "$MAX_FAILURES" ]; then
  echo "{\"event\": \"monitor_8765_repeated_failures\", \"timestamp\": \"$TS\", \"failure_count\": $count}" >> "$ESCALATION_FILE"
  echo "[$TS] ESCALATION: $count failures; wrote to $ESCALATION_FILE" >> "$LOG"
fi
exit 1
