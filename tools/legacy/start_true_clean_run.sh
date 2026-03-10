#!/usr/bin/env bash
# Start a true clean run: no migration, no backfill, no restore. Empty active state at start.
# Use after archive/remove of state.json, run.log, events.jsonl (see CEO directive true clean run).
# Requires: status.json present (run from mint-ladder-bot root).

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
RUNTIME_ROOT="${RUNTIME_ROOT:-$(cd "$PROJECT_ROOT/.." && pwd)/runtime}"
DATA_DIR="${DATA_DIR:-$RUNTIME_ROOT/projects/mint_ladder_bot}"
mkdir -p "$DATA_DIR"
cd "$PROJECT_ROOT"
export CLEAN_START=1
# Disable backfill/migration reintroduction
unset TX_BACKFILL_ONCE
PYTHON="${PYTHON:-.venv/bin/python3}"
exec "$PYTHON" -m mint_ladder_bot.main run --status "$DATA_DIR/status.json" --state "$DATA_DIR/state.json" "$@"
