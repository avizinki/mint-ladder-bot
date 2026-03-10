#!/usr/bin/env bash
# Stop mint-ladder-bot runner only (no deletions).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PROJECT_RUNTIME="${PROJECT_RUNTIME:-"$PROJECT_ROOT/runtime/projects/mint_ladder_bot"}"
LOG_DIR="${LOG_DIR:-"$PROJECT_ROOT/runtime/logs/mint-ladder-bot"}"
PID_FILE="${PID_FILE:-"$PROJECT_RUNTIME/runner.pid"}"

echo "[stop_runner] PROJECT_ROOT=$PROJECT_ROOT"
echo "[stop_runner] PROJECT_RUNTIME=$PROJECT_RUNTIME"
echo "[stop_runner] LOG_DIR=$LOG_DIR"
echo "[stop_runner] PID_FILE=$PID_FILE"

PIDS=""

# Primary: pidfile-based shutdown (most precise).
if [ -f "$PID_FILE" ]; then
  PID_FROM_FILE="$(cat "$PID_FILE" 2>/dev/null || echo "")"
  if [ -n "$PID_FROM_FILE" ] && kill -0 "$PID_FROM_FILE" 2>/dev/null; then
    PIDS="$PID_FROM_FILE"
    echo "[stop_runner] Found runner PID from pidfile: $PID_FROM_FILE"
  else
    echo "[stop_runner] Stale or dead PID in $PID_FILE (PID=$PID_FROM_FILE). Removing pidfile."
    rm -f "$PID_FILE"
  fi
fi

# Fallback: pattern match if no live PID from pidfile.
if [ -z "$PIDS" ]; then
  PIDS="$(pgrep -f \"python.*mint_ladder_bot.main run\" 2>/dev/null || true)"
fi

if [ -z "$PIDS" ]; then
  echo "[stop_runner] No runner process found."
  exit 0
fi

echo "[stop_runner] Sending SIGTERM to runner PIDs: $PIDS"
for pid in $PIDS; do
  kill "$pid" 2>/dev/null || true
done

sleep 2
PIDS2="$(pgrep -f \"python.*mint_ladder_bot.main run\" 2>/dev/null || true)"
if [ -n "$PIDS2" ]; then
  echo "[stop_runner] PIDs still running after SIGTERM, sending SIGKILL: $PIDS2"
  for pid in $PIDS2; do
    kill -9 "$pid" 2>/dev/null || true
  done
  sleep 1
fi

PIDS3="$(pgrep -f \"python.*mint_ladder_bot.main run\" 2>/dev/null || true)"
if [ -n "$PIDS3" ]; then
  echo "[stop_runner] WARNING: runner still appears to be running after SIGKILL; manual investigation required."
  exit 1
fi

# Clean up pidfile if process is gone.
if [ -f "$PID_FILE" ]; then
  rm -f "$PID_FILE" || true
fi

echo "[stop_runner] Runner stopped (state.json and status.json preserved)."

