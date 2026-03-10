#!/usr/bin/env bash
# CI dry-run: run-multi --simulation, one cycle or timeout. No live execution, no secrets.
# Run from repo root or mint-ladder-bot root. Exits 0 on success.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$BOT_ROOT"

# Activate venv if present
if [ -f ".venv/bin/activate" ]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

# Prefer --max-cycles 1; fallback to timeout 60 if option not supported
if python -m mint_ladder_bot.main run-multi --simulation --max-cycles 1 2>/dev/null; then
  exit 0
fi

# --max-cycles not yet available: run with timeout so process exits
timeout 60 python -m mint_ladder_bot.main run-multi --simulation
rc=$?
# 0 = normal exit, 124 = killed by timeout (expected when no --max-cycles)
if [ "$rc" = 0 ] || [ "$rc" = 124 ]; then
  exit 0
fi
exit "$rc"
