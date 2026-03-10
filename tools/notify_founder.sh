#!/usr/bin/env bash
# Notify founder: Telegram is the PRIMARY completion channel when configured; Mac is secondary.
# No secrets in messages. Safe to call from scripts / workforce / completion hooks.
# Usage: notify_founder.sh "Short message" [title]
#   or:  NOTIFY_TITLE="Custom" notify_founder.sh "Message"

set -euo pipefail

# Load .env from project root if present (so TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID work when script run from project)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/.env" 2>/dev/null || true
  set +a
fi

MESSAGE="${1:-}"
TITLE="${2:-${NOTIFY_TITLE:-Mint Ladder}}"

# Sanitize: single line, no control chars (safe for osascript and Telegram)
MESSAGE="$(printf '%s' "$MESSAGE" | tr -d '\n\r' | head -c 500)"
if [ -z "$MESSAGE" ]; then
  echo "Usage: $0 \"message\" [title]" >&2
  exit 1
fi

# --- Telegram (PRIMARY completion channel when configured) ---
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
TG_SENT=false
if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
  TG_TEXT="$(printf '%s' "$MESSAGE" | sed 's/\\/\\\\/g; s/"/\\"/g')"
  BODY="{\"chat_id\": \"$TELEGRAM_CHAT_ID\", \"text\": \"$TG_TEXT\"}"
  _tg_send() {
    curl -s -w "\n%{http_code}" -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -H "Content-Type: application/json" \
      -d "$BODY" \
      --max-time 10 2>/dev/null
  }
  TG_RESP="$(_tg_send)" || true
  TG_HTTP="$(echo "$TG_RESP" | tail -n1)"
  TG_BODY="$(echo "$TG_RESP" | sed '$d')"
  if [ "$TG_HTTP" = "200" ] && echo "$TG_BODY" | grep -q '"ok":true'; then
    TG_SENT=true
    echo "Telegram: sent" >&2
  else
    # Retry once
    sleep 1
    TG_RESP="$(_tg_send)" || true
    TG_HTTP="$(echo "$TG_RESP" | tail -n1)"
    TG_BODY="$(echo "$TG_RESP" | sed '$d')"
    if [ "$TG_HTTP" = "200" ] && echo "$TG_BODY" | grep -q '"ok":true'; then
      TG_SENT=true
      echo "Telegram: sent (retry)" >&2
    fi
  fi
  if [ "$TG_SENT" = false ]; then
    echo "Telegram: failed after retry (check token/chat_id or API)" >&2
    REPO_ROOT="$(cd "$PROJECT_ROOT/.." && pwd)"
    RUNTIME_ROOT="${RUNTIME_ROOT:-$REPO_ROOT/runtime}"
    mkdir -p "$RUNTIME_ROOT"
    TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "{\"ts\": \"$TS\", \"event\": \"telegram_delivery_failed\", \"message_preview\": \"${MESSAGE:0:80}\"}" >> "${RUNTIME_ROOT}/notification_alert.jsonl"
  fi
fi

# --- macOS notification (secondary; optional) ---
if [ "$(uname -s)" = "Darwin" ]; then
  SAFE_MSG="$(printf '%s' "$MESSAGE" | sed 's/\\/\\\\/g; s/"/\\"/g')"
  SAFE_TITLE="$(printf '%s' "$TITLE" | sed 's/\\/\\\\/g; s/"/\\"/g')"
  osascript -e "display notification \"$SAFE_MSG\" with title \"$SAFE_TITLE\"" 2>/dev/null || true
fi

exit 0
