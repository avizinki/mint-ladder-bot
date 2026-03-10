#!/usr/bin/env bash
# CEO clean live start:
# 1) Stop runner + dashboard
# 2) Delete ALL generated runtime files (state/status/events/logs/health)
# 3) Create fresh status.json from wallet
# 4) Create fresh state.json from status.json
# 5) Start live runner
# 6) Start dashboard
# 7) Print verification summary only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
PROJECT_RUNTIME="${PROJECT_RUNTIME:-"$PROJECT_ROOT/runtime/projects/mint_ladder_bot"}"
LOG_DIR="${LOG_DIR:-"$PROJECT_ROOT/runtime/logs/mint-ladder-bot"}"

# Shared ops helpers
# shellcheck source=/dev/null
source "$SCRIPT_DIR/common.sh"

STATE_FILE="$PROJECT_RUNTIME/state.json"
STATUS_FILE="$PROJECT_RUNTIME/status.json"

PY_BIN="$(ops_resolve_python)"

###############################################################################
# A. STOP PROCESSES (via unified ops scripts)
###############################################################################

"$SCRIPT_DIR/stop_runner.sh" || true
"$SCRIPT_DIR/stop_dashboard.sh" || true

###############################################################################
# B. DELETE GENERATED RUNTIME FILES
###############################################################################

mkdir -p "$PROJECT_RUNTIME" "$LOG_DIR"

# runtime/projects/mint_ladder_bot/*
(
  cd "$PROJECT_RUNTIME"
  shopt -s nullglob
  rm -f \
    state.json \
    state.json.bak* \
    status.json \
    events.jsonl \
    health_status.json \
    status_runtime.json \
    runner.lock \
    *.tmp \
    *.bak.tmp
) || true

# runtime/logs/mint-ladder-bot/*
(
  cd "$LOG_DIR"
  shopt -s nullglob
  rm -f \
    run.log \
    *.tmp
) || true

###############################################################################
# C. CREATE FRESH status.json FROM WALLET
###############################################################################

# Derive wallet pubkey from PRIVATE_KEY_BASE58 in .env
WALLET_PUBKEY="$("$PY_BIN" - << 'PY'
from pathlib import Path
from mint_ladder_bot.main import _load_env_file
from mint_ladder_bot.wallet_manager import resolve_identity

root = Path(__file__).resolve().parent.parent
_load_env_file(root / ".env")
print(resolve_identity(None))
PY
)"

mkdir -p "$PROJECT_RUNTIME"

"$PY_BIN" -m mint_ladder_bot.main status \
  --wallet "$WALLET_PUBKEY" \
  --out "$STATUS_FILE"

###############################################################################
# D. CREATE FRESH state.json FROM status.json
###############################################################################

"$PY_BIN" - << 'PY'
from pathlib import Path
from datetime import datetime, timezone

from mint_ladder_bot.config import Config
from mint_ladder_bot.models import RuntimeState, StatusFile
from mint_ladder_bot.strategy import compute_trading_bag
from mint_ladder_bot.state import ensure_mint_state, save_state_atomic

project_root = Path(__file__).resolve().parent.parent
runtime_dir = project_root / "runtime" / "projects" / "mint_ladder_bot"
status_path = runtime_dir / "status.json"
state_path = runtime_dir / "state.json"

config = Config()
status = StatusFile.model_validate_json(status_path.read_text())

state = RuntimeState(
    version=1,
    started_at=datetime.now(tz=timezone.utc),
    status_file=str(status_path),
    wallet=status.wallet,
    sol=status.sol,
    mints={},
)

for sm in status.mints:
    balance_raw = sm.balance_raw
    entry = sm.entry
    entry_price = getattr(entry, "entry_price_sol_per_token", 0.0) if entry else 0.0
    entry_source = None
    if entry is not None and getattr(entry, "entry_source", None) and str(entry.entry_source) != "unknown":
        entry_source = entry.entry_source
    trading_bag_raw, moonbag_raw = compute_trading_bag(str(balance_raw), config.trading_bag_pct)
    ensure_mint_state(
        state=state,
        mint=sm.mint,
        entry_price_sol_per_token=float(entry_price or 0.0),
        trading_bag_raw=trading_bag_raw,
        moonbag_raw=moonbag_raw,
        entry_source=entry_source,
    )

save_state_atomic(state_path, state)
PY

###############################################################################
# E. START LIVE RUNNER (via unified ops script)
###############################################################################

"$SCRIPT_DIR/start_runner.sh"
RUNNER_PID="$(cat "$PROJECT_RUNTIME/runner.pid" 2>/dev/null || echo "")"

###############################################################################
# F. START DASHBOARD (via unified ops script)
###############################################################################

"$SCRIPT_DIR/start_dashboard.sh"
DASH_PID="$(cat "$PROJECT_RUNTIME/dashboard.pid" 2>/dev/null || echo "")"

sleep 5

###############################################################################
# G. VERIFICATION OUTPUT
###############################################################################

STATUS_MINTS=$("$PY_BIN" - << 'PY'
import json, pathlib
p = pathlib.Path("runtime/projects/mint_ladder_bot/status.json")
d = json.loads(p.read_text())
print(len(d.get("mints") or []))
PY
)

STATE_SUMMARY=$("$PY_BIN" - << 'PY'
import json, pathlib
p = pathlib.Path("runtime/projects/mint_ladder_bot/state.json")
if not p.exists():
    print("STATE_MISSING")
else:
    d = json.loads(p.read_text())
    wallet = d.get("wallet")
    mints = d.get("mints") or {}
    lots = 0
    for ms in mints.values():
        lots += len(ms.get("lots") or [])
    print(wallet, len(mints), lots)
PY
)

if command -v curl >/dev/null 2>&1; then
  DASH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/ || echo "000")
else
  DASH_STATUS="curl_unavailable"
fi

echo "wallet_pubkey: $WALLET_PUBKEY"
echo "status_mint_count: $STATUS_MINTS"
echo "state_summary: $STATE_SUMMARY"
echo "runner_pid: $RUNNER_PID"
echo "dashboard_pid: $DASH_PID"
echo "dashboard_http_status: $DASH_STATUS"

