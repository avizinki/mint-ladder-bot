#!/usr/bin/env bash
# Verify Telegram delivery. Exit 0 and print "Telegram: OK" if message was sent.
# Usage: ./verify_notification_delivery.sh "Message body"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/.env" 2>/dev/null || true
  set +a
fi

MESSAGE="${1:-Verification test}"
MESSAGE="$(printf '%s' "$MESSAGE" | tr -d '\n\r' | head -c 500)"

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "Telegram: NOT_CONFIGURED" >&2
  exit 1
fi

TG_TEXT="$(printf '%s' "$MESSAGE" | sed 's/\\/\\\\/g; s/"/\\"/g')"
BODY="{\"chat_id\": \"$TELEGRAM_CHAT_ID\", \"text\": \"$TG_TEXT\"}"
TG_RESP="$(curl -s -w "\n%{http_code}" -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -H "Content-Type: application/json" \
  -d "$BODY" \
  --max-time 10 2>/dev/null)" || true
TG_HTTP="$(echo "$TG_RESP" | tail -n1)"
TG_BODY="$(echo "$TG_RESP" | sed '$d')"

if [ "$TG_HTTP" = "200" ] && echo "$TG_BODY" | grep -q '"ok":true'; then
  echo "Telegram: OK"
  exit 0
fi
echo "Telegram: FAILED" >&2
exit 1
