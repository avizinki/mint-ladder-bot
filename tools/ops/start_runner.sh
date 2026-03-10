#!/usr/bin/env bash
# Start mint-ladder-bot runner using existing state.json and status.json (no deletions).
# Deterministic: explicit interpreter, cwd, pidfile, and health check.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PROJECT_RUNTIME="${PROJECT_RUNTIME:-"$PROJECT_ROOT/runtime/projects/mint_ladder_bot"}"
LOG_DIR="${LOG_DIR:-"$PROJECT_ROOT/runtime/logs/mint-ladder-bot"}"
PID_FILE="${PID_FILE:-"$PROJECT_RUNTIME/runner.pid"}"

# Shared ops helpers
# shellcheck source=/dev/null
source "$SCRIPT_DIR/common.sh"

ops_ensure_runtime_and_logs

echo "[start_runner] PROJECT_ROOT=$PROJECT_ROOT"
echo "[start_runner] PROJECT_RUNTIME=$PROJECT_RUNTIME"
echo "[start_runner] LOG_DIR=$LOG_DIR"
echo "[start_runner] PID_FILE=$PID_FILE"

STATUS_FILE="$PROJECT_RUNTIME/status.json"
STATE_FILE="$PROJECT_RUNTIME/state.json"

if [ ! -f "$STATUS_FILE" ]; then
  echo "[start_runner] ERROR: $STATUS_FILE missing. Aborting."
  exit 1
fi
if [ ! -f "$STATE_FILE" ]; then
  echo "[start_runner] ERROR: $STATE_FILE missing. Aborting."
  exit 1
fi

if [ -f "$PROJECT_ROOT/STOP" ]; then
  echo "[start_runner] STOP file present at $PROJECT_ROOT/STOP. Refusing to start runner."
  exit 2
fi

if [ -f "$PROJECT_ROOT/.env" ]; then
  echo "[start_runner] Loading .env from $PROJECT_ROOT/.env"
  set -a
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/.env" 2>/dev/null || true
  set +a
fi

PYTHON="$(ops_resolve_python)"
echo "[start_runner] Using PYTHON=$PYTHON"

# If a previous PID file exists but process is dead, clean it up; if alive, refuse to start a duplicate.
if [ -f "$PID_FILE" ]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || echo "")"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[start_runner] ERROR: runner already appears to be running with PID $OLD_PID (from $PID_FILE). Refusing to start another."
    exit 3
  else
    echo "[start_runner] Stale PID file found at $PID_FILE (PID=$OLD_PID). Removing."
    rm -f "$PID_FILE"
  fi
fi

echo "[start_runner] Starting runner (background, cwd=$PROJECT_ROOT)..."
(
  cd "$PROJECT_ROOT"
  nohup "$PYTHON" -m mint_ladder_bot.main run \
    --status "$STATUS_FILE" \
    --state "$STATE_FILE" \
    >> "$LOG_DIR/run.log" 2>&1 &
  echo $! > "$PID_FILE"
) || {
  echo "[start_runner] ERROR: failed to launch runner subprocess."
  exit 1
}

sleep 3

NEW_PID="$(cat "$PID_FILE" 2>/dev/null || echo "")"
if [ -z "$NEW_PID" ]; then
  echo "[start_runner] ERROR: PID file $PID_FILE not written. Startup failed."
  exit 1
fi

if ! kill -0 "$NEW_PID" 2>/dev/null; then
  echo "[start_runner] ERROR: runner process PID $NEW_PID is not alive after startup. Inspect $LOG_DIR/run.log."
  exit 1
fi

echo "[start_runner] Runner started with PID $NEW_PID"
echo "[start_runner] Tail log with: tail -n 50 \"$LOG_DIR/run.log\""
