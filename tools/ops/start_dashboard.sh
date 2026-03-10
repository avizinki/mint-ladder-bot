#!/usr/bin/env bash
# Start dashboard HTTP server only (no deletions).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PROJECT_RUNTIME="${PROJECT_RUNTIME:-"$PROJECT_ROOT/runtime/projects/mint_ladder_bot"}"
LOG_DIR="${LOG_DIR:-"$PROJECT_ROOT/runtime/logs/mint-ladder-bot"}"
PID_FILE="${DASHBOARD_PID_FILE:-"$PROJECT_RUNTIME/dashboard.pid"}"

# Shared ops helpers
# shellcheck source=/dev/null
source "$SCRIPT_DIR/common.sh"

ops_ensure_runtime_and_logs

echo "[start_dashboard] PROJECT_ROOT=$PROJECT_ROOT"
echo "[start_dashboard] PROJECT_RUNTIME=$PROJECT_RUNTIME"
echo "[start_dashboard] LOG_DIR=$LOG_DIR"
echo "[start_dashboard] PID_FILE=$PID_FILE"

if [ -f "$PROJECT_ROOT/.env" ]; then
  echo "[start_dashboard] Loading .env from $PROJECT_ROOT/.env"
  set -a
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/.env" 2>/dev/null || true
  set +a
fi

PYTHON="$(ops_resolve_python)"
echo "[start_dashboard] Using PYTHON=$PYTHON"

DASHBOARD_PORT="${DASHBOARD_PORT:-8765}"

echo "[start_dashboard] Starting dashboard (background, cwd=$PROJECT_ROOT) on port $DASHBOARD_PORT..."

if [ -f "$PID_FILE" ]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || echo "")"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[start_dashboard] ERROR: dashboard already appears to be running with PID $OLD_PID (from $PID_FILE). Refusing to start another."
    exit 3
  else
    echo "[start_dashboard] Stale PID file found at $PID_FILE (PID=$OLD_PID). Removing."
    rm -f "$PID_FILE"
  fi
fi

(
  cd "$PROJECT_ROOT"
  nohup "$PYTHON" -m mint_ladder_bot.dashboard_server \
    --data-dir "$PROJECT_RUNTIME" \
    --port "$DASHBOARD_PORT" \
    --host 127.0.0.1 \
    >> "$LOG_DIR/dashboard.log" 2>&1 &
  echo $! > "$PID_FILE"
) || {
  echo "[start_dashboard] ERROR: failed to launch dashboard subprocess."
  exit 1
}

sleep 3

NEW_PID="$(cat "$PID_FILE" 2>/dev/null || echo "")"
if [ -z "$NEW_PID" ]; then
  echo "[start_dashboard] ERROR: PID file $PID_FILE not written. Startup failed."
  exit 1
fi

if ! kill -0 "$NEW_PID" 2>/dev/null; then
  echo "[start_dashboard] ERROR: dashboard process PID $NEW_PID is not alive after startup. Inspect $LOG_DIR/dashboard.log."
  exit 1
fi

# Basic HTTP health check
if command -v curl >/dev/null 2>&1; then
  STATUS_CODE="$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${DASHBOARD_PORT}/runtime/dashboard" || echo "000")"
  if [ "$STATUS_CODE" != "200" ]; then
    echo "[start_dashboard] ERROR: dashboard HTTP health check failed (status=$STATUS_CODE). Inspect $LOG_DIR/dashboard.log."
    exit 1
  fi
fi

echo "[start_dashboard] Dashboard started with PID $NEW_PID on port $DASHBOARD_PORT"

