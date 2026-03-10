#!/bin/bash
# Real clean run: archive ALL runtime artifacts and prepare for a fresh live run
# with NO historical lots. Do NOT merge/backfill/restore from archive.
#
# Usage:
#   1. ./tools/real_clean_run.sh              # Archive only; then you regenerate status and start manually.
#   2. ./tools/real_clean_run.sh --regen-status  # Archive + regenerate status.json from archived wallet; then start manually.
#
# After running this script:
#   - Start runtime with: CLEAN_START=1 (and do NOT set TX_BACKFILL_ONCE or BACKFILL_LOT_TX_ONCE)
#   - Example: CLEAN_START=1 .venv/bin/python -m mint_ladder_bot.main run --status status.json --state state.json
#   - state.json will be created on first run from status (mints populated, lots empty).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
RUNTIME_ROOT="${RUNTIME_ROOT:-$(cd "$PROJECT_ROOT/.." && pwd)/runtime}"
DATA_DIR="${DATA_DIR:-$RUNTIME_ROOT/projects/mint_ladder_bot}"
mkdir -p "$DATA_DIR"
cd "$PROJECT_ROOT"

TS="$(date -u +%Y%m%d_%H%M%S)"
ARCHIVE_DIR="${RUNTIME_ROOT}/archive/real_clean_run_${TS}"
REGEN_STATUS=false
for arg in "$@"; do
  case "$arg" in
    --regen-status) REGEN_STATUS=true ;;
  esac
done

echo "=== Real clean run: archive to ${ARCHIVE_DIR} ==="

# 1. Stop all related processes
echo "Stopping runtime and watchdog..."
pkill -f "mint_ladder_bot.main run" 2>/dev/null || true
pkill -f "watchdog.py" 2>/dev/null || true
sleep 2
for pid in $(pgrep -f "mint_ladder_bot.main run" 2>/dev/null); do kill -9 "$pid" 2>/dev/null || true; done
for pid in $(pgrep -f "watchdog.py" 2>/dev/null); do kill -9 "$pid" 2>/dev/null || true; done
sleep 1
if pgrep -f "mint_ladder_bot.main run" >/dev/null 2>&1; then
  echo "ERROR: Runtime still running; abort."
  exit 1
fi

# 2. Create archive dir and move ALL runtime artifacts
mkdir -p "$ARCHIVE_DIR"

for f in state.json status.json run.log health_status.json uptime_alerts.jsonl restart_log.jsonl alerts.json .lane_state.json events.jsonl safety_state.json escalation.jsonl; do
  if [ -f "$DATA_DIR/$f" ]; then
    mv "$DATA_DIR/$f" "$ARCHIVE_DIR/" 2>/dev/null || true
  fi
done
mkdir -p "$ARCHIVE_DIR/runtime"
if [ -f "$PROJECT_ROOT/runtime/config_hash.json" ]; then
  mv "$PROJECT_ROOT/runtime/config_hash.json" "$ARCHIVE_DIR/runtime/" 2>/dev/null || true
fi
for f in state.json.bak.1 state.json.bak.2 state.json.bak.3; do
  if [ -f "$DATA_DIR/$f" ]; then
    mv "$DATA_DIR/$f" "$ARCHIVE_DIR/" 2>/dev/null || true
  fi
done

# Optional: temp/dashboard-derived files in project root
for f in dashboard_payload.json; do
  if [ -f "$PROJECT_ROOT/$f" ]; then
    mv "$PROJECT_ROOT/$f" "$ARCHIVE_DIR/" 2>/dev/null || true
  fi
done

echo "Archived to ${ARCHIVE_DIR}"

# 3. Regenerate status.json from wallet (from archived status or .env)
if [ "$REGEN_STATUS" = true ]; then
  if [ ! -f "${ARCHIVE_DIR}/status.json" ]; then
    echo "ERROR: --regen-status requires status.json in archive to read wallet. Run without --regen-status or copy status.json back temporarily to read wallet."
    exit 1
  fi
  WALLET=""
  if command -v jq >/dev/null 2>&1; then
    WALLET="$(jq -r '.wallet // empty' "${ARCHIVE_DIR}/status.json")"
  fi
  if [ -z "$WALLET" ]; then
    if [ -f "$PROJECT_ROOT/.env" ]; then
      set -a
      # shellcheck source=/dev/null
      source "$PROJECT_ROOT/.env" 2>/dev/null || true
      set +a
      WALLET="${WALLET_PUBKEY:-}"
    fi
  fi
  if [ -z "$WALLET" ]; then
    echo "ERROR: Could not get wallet from archive/status.json or .env WALLET_PUBKEY. Set WALLET_PUBKEY in .env or pass wallet manually."
    exit 1
  fi
  PYTHON="${PROJECT_ROOT}/.venv/bin/python"
  [ ! -x "$PYTHON" ] && PYTHON="python3"
  echo "Regenerating status.json for wallet ${WALLET:0:8}..."
  "$PYTHON" -m mint_ladder_bot.main status --wallet "$WALLET" --out "${DATA_DIR}/status.json"
  echo "status.json created at ${DATA_DIR}/status.json."
fi

echo ""
echo "=== Next steps ==="
echo "1. Ensure CLEAN_START=1 and do NOT set TX_BACKFILL_ONCE or BACKFILL_LOT_TX_ONCE for this run."
echo "2. If you did not use --regen-status, create fresh status.json (e.g. mint_ladder_bot status --wallet <WALLET> --out $DATA_DIR/status.json)."
echo "3. Start one process only:"
echo "   CLEAN_START=1 $PROJECT_ROOT/.venv/bin/python -m mint_ladder_bot.main run --status $DATA_DIR/status.json --state $DATA_DIR/state.json"
echo "   (Add --monitor-only for monitor-only.)"
echo "4. Verify: curl -s http://127.0.0.1:8765/ ; state.json will be created with mints but no historical lots."
echo "5. Run 1-2 cycles; confirm browser, API, and state align with no old records."
exit 0
