#!/usr/bin/env bash
# Shared helpers for mint-ladder-bot ops scripts (runner + dashboard).
#
# Assumptions:
# - Caller has already set SCRIPT_DIR and PROJECT_ROOT.
# - PROJECT_RUNTIME and LOG_DIR are set or will be set by caller.

ops_resolve_python() {
  # Resolve the Python interpreter for all ops: prefer project venv python3 only.
  # Fail loudly if it does not exist or is not executable.
  local root="${PROJECT_ROOT:?PROJECT_ROOT not set}"
  local py="$root/.venv/bin/python3"
  if [ ! -x "$py" ]; then
    echo "[ops] ERROR: expected venv python3 at $py but it is missing or not executable."
    echo "[ops] Create the project virtualenv (with python3) or adjust ops configuration before retrying."
    exit 1
  fi
  printf '%s\n' "$py"
}

ops_ensure_runtime_and_logs() {
  # Ensure runtime and log directories exist.
  mkdir -p "$PROJECT_RUNTIME" "$LOG_DIR"
}

