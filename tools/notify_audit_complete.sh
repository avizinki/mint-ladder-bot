#!/usr/bin/env bash
# Send founder notification when dashboard audit + fixes are complete.
# Uses shared notify_founder.sh. Retries once on failure; on final failure
# logs and appends to uptime_alerts.jsonl.
# Usage: run from mint-ladder-bot root after audit/validation is done.
#   ./tools/notify_audit_complete.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
cd "$PROJECT_ROOT"

TITLE="Mint Ladder"
BODY="I'm done baby — dashboard audit + fixes complete. Ready for notification test."

# Data dir for uptime_alerts.jsonl (dashboard_server reads from same area when data_dir is set)
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

_alert_fail() {
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%S.%6N%:z 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "{\"alert_type\": \"audit_complete_notify_failed\", \"severity\": \"medium\", \"message\": \"Audit-complete notification failed after retry\", \"timestamp\": \"${ts}\"}" >> "$ALERTS_FILE"
  echo "Notification failed; logged to $ALERTS_FILE" >&2
}

if _send; then
  exit 0
fi

echo "First attempt failed; retrying once..." >&2
if _send; then
  exit 0
fi

echo "Audit-complete notification failed after retry" >&2
_alert_fail
exit 1
