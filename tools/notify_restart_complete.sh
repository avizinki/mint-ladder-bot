#!/usr/bin/env bash
# Send founder notification after restart + verification.
# Message: "I'm done baby — restart complete and fixes active. Ready for notification test."
# Retry once on failure; on final failure log to restart_log.jsonl and uptime_alerts.jsonl.
# Usage: run from mint-ladder-bot root after restart and health check.
#   ./tools/notify_restart_complete.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
cd "$PROJECT_ROOT"

TITLE="Mint Ladder"
BODY="I'm done baby — restart complete and fixes active. Ready for notification test."

RESTART_LOG="${PROJECT_ROOT}/restart_log.jsonl"
DATA_DIR="${MINT_LADDER_DATA_DIR:-$PROJECT_ROOT}"
ALERTS_FILE="${DATA_DIR}/uptime_alerts.jsonl"
mkdir -p "$(dirname "$ALERTS_FILE")"

_send() {
  if [ -x "$SCRIPT_DIR/notify_founder.sh" ]; then
    "$SCRIPT_DIR/notify_founder.sh" "$BODY" "$TITLE"
  else
    echo "notify_founder.sh missing or not executable" >&2
    return 1
  fi
}

_log_fail() {
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "{\"timestamp\": \"$ts\", \"event\": \"restart_complete_notify_failed\", \"message\": \"Notification failed after retry\"}" >> "$RESTART_LOG"
  echo "{\"alert_type\": \"restart_complete_notify_failed\", \"severity\": \"medium\", \"message\": \"Restart-complete notification failed after retry\", \"timestamp\": \"${ts}\"}" >> "$ALERTS_FILE"
  echo "Notification failed; logged to $RESTART_LOG and $ALERTS_FILE" >&2
}

if _send; then
  exit 0
fi

echo "First attempt failed; retrying once..." >&2
if _send; then
  exit 0
fi

_log_fail
exit 1
