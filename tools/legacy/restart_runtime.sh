#!/bin/bash
# Restart mint-ladder-bot runtime and optional service on 8765.
# Respects STOP file unless --override-stop. Logs attempts; no secrets echoed.
# Default: monitor-only. Set RESTART_LIVE=1 for live (when approved).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
# Centralized runtime root and data/log dirs
RUNTIME_ROOT="${RUNTIME_ROOT:-$(cd "$PROJECT_ROOT/.." && pwd)/runtime}"
DATA_DIR="${DATA_DIR:-$RUNTIME_ROOT/projects/mint_ladder_bot}"
LOG_DIR="${LOG_DIR:-$RUNTIME_ROOT/logs/mint-ladder-bot}"
mkdir -p "$DATA_DIR" "$LOG_DIR"
cd "$PROJECT_ROOT"

OVERRIDE_STOP=false
LIVE=false
for arg in "$@"; do
  case "$arg" in
    --override-stop) OVERRIDE_STOP=true ;;
    --live)          LIVE=true ;;
  esac
done

RESTART_LOG="${RUNTIME_ROOT}/restart_log.jsonl"
RUNTIME_RESTART_LOG="${RUNTIME_ROOT}/restart_log.jsonl"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
REASON="${RESTART_REASON:-unknown}"

# 1. STOP guard
if [ -f "$PROJECT_ROOT/STOP" ] && [ "$OVERRIDE_STOP" != true ]; then
  echo "STOP file present; refusing restart. Use --override-stop to override."
  echo "{\"timestamp\": \"$TS\", \"event\": \"skip_stop_file\"}" >> "$RESTART_LOG"
  [ -x "$SCRIPT_DIR/notify_founder.sh" ] && "$SCRIPT_DIR/notify_founder.sh" "Restart skipped (STOP file present)" "Mint Ladder"
  exit 2
fi

# 2. Log restart reason (e.g. CONFIG_CHANGE)
echo "{\"timestamp\": \"$TS\", \"event\": \"restart\", \"reason\": \"$REASON\"}" >> "$RESTART_LOG"
if [ "$REASON" = "config_change" ] || [ "$REASON" = "env_hash_changed" ]; then
  echo "{\"event\": \"config_restart\", \"reason\": \"env_hash_changed\", \"timestamp\": \"$TS\"}" >> "$RUNTIME_RESTART_LOG"
fi

# 3. Optional archive run.log
if [ -f "$LOG_DIR/run.log" ]; then
  mkdir -p "$RUNTIME_ROOT/archive"
  mv "$LOG_DIR/run.log" "$RUNTIME_ROOT/archive/run_$(date +%Y%m%d_%H%M).log"
fi

# 4. Stop stale bot process (SIGTERM then SIGKILL)
PIDS="$(pgrep -f "mint_ladder_bot.main run" 2>/dev/null || true)"
if [ -n "$PIDS" ]; then
  for pid in $PIDS; do
    kill "$pid" 2>/dev/null || true
  done
  sleep 2
  PIDS2="$(pgrep -f "mint_ladder_bot.main run" 2>/dev/null || true)"
  if [ -n "$PIDS2" ]; then
    for pid in $PIDS2; do
      kill -9 "$pid" 2>/dev/null || true
    done
    sleep 1
  fi
  PIDS3="$(pgrep -f "mint_ladder_bot.main run" 2>/dev/null || true)"
  if [ -n "$PIDS3" ]; then
    echo "Bot process still running after SIGKILL; aborting restart."
    echo "{\"timestamp\": \"$TS\", \"event\": \"stop_failed\"}" >> "$RESTART_LOG"
    [ -x "$SCRIPT_DIR/notify_founder.sh" ] && "$SCRIPT_DIR/notify_founder.sh" "Runtime restart failed — investigation required" "Mint Ladder"
    exit 1
  fi
fi

# 5. Optional start service on 8765 (no echo of cmd)
if [ -n "${SERVICE_8765_CMD:-}" ]; then
  eval "nohup $SERVICE_8765_CMD" > /dev/null 2>&1 &
  sleep 3
fi

# 6. Load .env so RPC_ENDPOINT and other vars are set for the new process
if [ -f "${PROJECT_ROOT}/.env" ]; then
  set -a
  # shellcheck source=/dev/null
  source "${PROJECT_ROOT}/.env" 2>/dev/null || true
  set +a
fi

# 7. Start bot (monitor-only unless RESTART_LIVE=1 or --live)
MONITOR_FLAG="--monitor-only"
if [ "${RESTART_LIVE:-0}" = "1" ] || [ "$LIVE" = true ]; then
  MONITOR_FLAG=""
fi

PYTHON="${PROJECT_ROOT}/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

nohup "$PYTHON" -m mint_ladder_bot.main run --status "$DATA_DIR/status.json" --state "$DATA_DIR/state.json" $MONITOR_FLAG >> "${LOG_DIR}/run.log" 2>&1 &

# 8. Update config hash so watchdog knows current .env state
mkdir -p "$RUNTIME_ROOT"
if [ -f "$PROJECT_ROOT/.env" ]; then
  HASH="$(openssl dgst -sha256 "$PROJECT_ROOT/.env" 2>/dev/null | awk '{print $2}')"
  if [ -n "$HASH" ]; then
    echo "{\"env_hash\": \"$HASH\", \"timestamp\": \"$TS\"}" > "$RUNTIME_ROOT/config_hash.json"
  fi
fi

MONITOR_ONLY_VAL=false; [ -n "$MONITOR_FLAG" ] && MONITOR_ONLY_VAL=true
echo "{\"timestamp\": \"$TS\", \"event\": \"started\", \"reason\": \"$REASON\", \"monitor_only\": $MONITOR_ONLY_VAL}" >> "$RESTART_LOG"
[ -x "$SCRIPT_DIR/notify_founder.sh" ] && "$SCRIPT_DIR/notify_founder.sh" "Runtime restarted successfully" "Mint Ladder"
exit 0
