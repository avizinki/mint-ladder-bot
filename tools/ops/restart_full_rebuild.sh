#!/usr/bin/env bash
# Full rebuild restart: stop runner + dashboard, archive and remove state/status and transient artifacts,
# then delegate to scratch/rebuild tools to reconstruct status/state before starting both.
#
# WARNING: This is destructive to current runtime state and status snapshots. Use only for corrupted state recovery.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PROJECT_RUNTIME="${PROJECT_RUNTIME:-"$PROJECT_ROOT/runtime/projects/mint_ladder_bot"}"
RUNTIME_ROOT="${RUNTIME_ROOT:-"$PROJECT_ROOT/runtime"}"
ARCHIVE_DIR="${ARCHIVE_DIR:-"$RUNTIME_ROOT/archive"}"
LOG_DIR="${LOG_DIR:-"$PROJECT_ROOT/runtime/logs/mint-ladder-bot"}"

echo "[restart_full_rebuild] PROJECT_ROOT=$PROJECT_ROOT"
echo "[restart_full_rebuild] PROJECT_RUNTIME=$PROJECT_RUNTIME"
echo "[restart_full_rebuild] LOG_DIR=$LOG_DIR"
echo "[restart_full_rebuild] FULL REBUILD RESTART: archiving and removing state.json, status.json, backups and transient artifacts."

mkdir -p "$ARCHIVE_DIR"

TS="$(date -u +%Y%m%d_%H%M%S)"
ARCHIVE_RUN_DIR="$ARCHIVE_DIR/full_rebuild_$TS"
mkdir -p "$ARCHIVE_RUN_DIR"

echo "[restart_full_rebuild] Stopping runner..."
"$SCRIPT_DIR/stop_runner.sh"

echo "[restart_full_rebuild] Stopping dashboard..."
"$SCRIPT_DIR/stop_dashboard.sh"

cd "$PROJECT_RUNTIME"

for f in state.json state.json.bak.* status.json events.jsonl health_status.json status_runtime.json runner.lock *.tmp *.bak.tmp; do
  if ls $f >/dev/null 2>&1; then
    echo "[restart_full_rebuild] Archiving $f to $ARCHIVE_RUN_DIR"
    mv $f "$ARCHIVE_RUN_DIR/" 2>/dev/null || true
  fi
done

mkdir -p "$LOG_DIR"
cd "$LOG_DIR"
for f in run.log run.log.tmp *.tmp; do
  if ls $f >/dev/null 2>&1; then
    echo "[restart_full_rebuild] Archiving $f to $ARCHIVE_RUN_DIR"
    mv $f "$ARCHIVE_RUN_DIR/" 2>/dev/null || true
  fi
done

echo "[restart_full_rebuild] Archived previous runtime to $ARCHIVE_RUN_DIR"

echo "[restart_full_rebuild] Rebuilding status.json from chain history (read-only tools)."
cd "$PROJECT_ROOT"
PYTHON="${PROJECT_ROOT}/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

echo "[restart_full_rebuild] Running full_history_coverage_report.py (non-destructive)."
"$PYTHON" tools/full_history_coverage_report.py || true

echo "[restart_full_rebuild] Running full_history_scratch_rebuild.py to build fresh state.json from history."
"$PYTHON" tools/full_history_scratch_rebuild.py || {
  echo "[restart_full_rebuild] ERROR: full_history_scratch_rebuild.py failed. Manual intervention required."
  exit 1
}

echo "[restart_full_rebuild] Starting runner..."
"$SCRIPT_DIR/start_runner.sh"

echo "[restart_full_rebuild] Starting dashboard..."
"$SCRIPT_DIR/start_dashboard.sh"

echo "[restart_full_rebuild] Full rebuild restart complete. Verify reconciliation and dashboard before enabling trading."

