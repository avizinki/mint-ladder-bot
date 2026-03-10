#!/usr/bin/env bash
# Validate dashboard payload and 5-token truth. Requires dashboard on 8765.
# Usage: scripts/validate_truth.sh [--base-url http://127.0.0.1:8765]
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

BASE_URL="http://127.0.0.1:8765"
for i in "$@"; do
  if [[ "$i" == "--base-url" ]]; then shift; BASE_URL="$1"; shift; break; fi
done
[[ -n "$1" && "$1" != --* ]] && BASE_URL="$1"

echo "[validate_truth] Checking dashboard at $BASE_URL..."
if ! curl -sf --connect-timeout 10 --max-time 15 -o /dev/null "$BASE_URL/"; then
  echo "[validate_truth] ERROR: Dashboard not responding at $BASE_URL" >&2
  exit 1
fi

echo "[validate_truth] Running 5-token validation (live API)..."
PYTHON="${PYTHON:-python3}"
if ! "$PYTHON" "$PROJECT_ROOT/tools/validate_five_token_truth.py" --base-url "$BASE_URL" --api-timeout 15 --api-retries 3; then
  echo "[validate_truth] Validation failed (see API_VALIDATION_FAIL or LOCAL_STATE_VALIDATION_FAIL above)." >&2
  exit 1
fi
echo "[validate_truth] Done."
