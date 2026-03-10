#!/usr/bin/env bash
# Soft restart: stop runner + dashboard, preserve state.json and status.json, then start both.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PROJECT_RUNTIME="${PROJECT_RUNTIME:-"$PROJECT_ROOT/runtime/projects/mint_ladder_bot"}"

echo "[restart_soft] PROJECT_ROOT=$PROJECT_ROOT"
echo "[restart_soft] PROJECT_RUNTIME=$PROJECT_RUNTIME"
echo "[restart_soft] SOFT RESTART: preserving state.json and status.json"

STATE_FILE="$PROJECT_RUNTIME/state.json"
STATUS_FILE="$PROJECT_RUNTIME/status.json"

if [ ! -f "$STATE_FILE" ] || [ ! -f "$STATUS_FILE" ]; then
  echo "[restart_soft] ERROR: state.json or status.json missing in $PROJECT_RUNTIME. Aborting."
  exit 1
fi

echo "[restart_soft] Stopping runner..."
"$SCRIPT_DIR/stop_runner.sh"

echo "[restart_soft] Stopping dashboard..."
"$SCRIPT_DIR/stop_dashboard.sh"

echo "[restart_soft] Starting runner..."
"$SCRIPT_DIR/start_runner.sh"

echo "[restart_soft] Starting dashboard..."
"$SCRIPT_DIR/start_dashboard.sh"

echo "[restart_soft] Soft restart complete. Verify runner via run.log and dashboard via /runtime/dashboard."

