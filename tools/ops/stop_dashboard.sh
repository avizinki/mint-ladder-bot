#!/usr/bin/env bash
# Stop dashboard HTTP server only (no deletions).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PROJECT_RUNTIME="${PROJECT_RUNTIME:-"$PROJECT_ROOT/runtime/projects/mint_ladder_bot"}"
PID_FILE="${DASHBOARD_PID_FILE:-"$PROJECT_RUNTIME/dashboard.pid"}"

echo "[stop_dashboard] PROJECT_ROOT=$PROJECT_ROOT"
echo "[stop_dashboard] PROJECT_RUNTIME=$PROJECT_RUNTIME"
echo "[stop_dashboard] PID_FILE=$PID_FILE"

PIDS=""

if [ -f "$PID_FILE" ]; then
  PID_FROM_FILE="$(cat "$PID_FILE" 2>/dev/null || echo "")"
  if [ -n "$PID_FROM_FILE" ] && kill -0 "$PID_FROM_FILE" 2>/dev/null; then
    PIDS="$PID_FROM_FILE"
    echo "[stop_dashboard] Found dashboard PID from pidfile: $PID_FROM_FILE"
  else
    echo "[stop_dashboard] Stale or dead PID in $PID_FILE (PID=$PID_FROM_FILE). Removing pidfile."
    rm -f "$PID_FILE"
  fi
fi

if [ -z "$PIDS" ]; then
  PIDS="$(pgrep -f \"python.*mint_ladder_bot.dashboard_server\" 2>/dev/null || true)"
fi

if [ -z "$PIDS" ]; then
  echo "[stop_dashboard] No dashboard process found."
  exit 0
fi

echo "[stop_dashboard] Sending SIGTERM to dashboard PIDs: $PIDS"
for pid in $PIDS; do
  kill "$pid" 2>/dev/null || true
done

sleep 2
PIDS2="$(pgrep -f \"python.*mint_ladder_bot.dashboard_server\" 2>/dev/null || true)"
if [ -n "$PIDS2" ]; then
  echo "[stop_dashboard] PIDs still running after SIGTERM, sending SIGKILL: $PIDS2"
  for pid in $PIDS2; do
    kill -9 "$pid" 2>/dev/null || true
  done
  sleep 1
fi

PIDS3="$(pgrep -f \"python.*mint_ladder_bot.dashboard_server\" 2>/dev/null || true)"
if [ -n "$PIDS3" ]; then
  echo "[stop_dashboard] WARNING: dashboard still appears to be running after SIGKILL; manual investigation required."
  exit 1
fi

if [ -f "$PID_FILE" ]; then
  rm -f "$PID_FILE" || true
fi

echo "[stop_dashboard] Dashboard stopped."

