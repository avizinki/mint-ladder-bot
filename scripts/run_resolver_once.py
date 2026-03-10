#!/usr/bin/env python3
"""
One-off run of the pending-price resolver. Uses current RPC_ENDPOINT (e.g. Helius).
Loads state, runs _resolve_pending_price_lots, saves state.
Run from project root: python3 scripts/run_resolver_once.py (or .venv/bin/python3).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Project root
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load .env if present (so RPC_ENDPOINT is set)
_env = PROJECT_ROOT / ".env"
if _env.exists():
    with open(_env, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    os.environ.setdefault(k, v)

from mint_ladder_bot.config import Config
from mint_ladder_bot.state import load_state, save_state_atomic
from mint_ladder_bot.runner import _resolve_pending_price_lots
from mint_ladder_bot.rpc import RpcClient


def count_pending(state) -> int:
    n = 0
    for ms in getattr(state, "mints", {}).values():
        for lot in getattr(ms, "lots", None) or []:
            if getattr(lot, "entry_confidence", None) == "pending_price_resolution":
                n += 1
    return n


def main() -> int:
    state_path = PROJECT_ROOT / "state.json"
    status_path = PROJECT_ROOT / "status.json"
    if not state_path.exists():
        print("state.json not found", file=sys.stderr)
        return 1
    if not status_path.exists():
        print("status.json not found", file=sys.stderr)
        return 1

    config = Config()
    state = load_state(state_path, status_path)
    pending_before = count_pending(state)
    print(f"Pending lots before: {pending_before}")

    wallet = getattr(state, "wallet", None)
    if not wallet:
        print("No wallet in state", file=sys.stderr)
        return 1

    # decimals_by_mint from status
    try:
        status_data = json.loads(status_path.read_text())
        mints_list = status_data.get("mints") or []
        decimals_by_mint = {}
        for m in mints_list:
            if isinstance(m, dict) and m.get("mint"):
                decimals_by_mint[m["mint"]] = m.get("decimals", 6)
    except Exception:
        decimals_by_mint = {}

    rpc = RpcClient(
        config.rpc_endpoint,
        timeout_s=getattr(config, "rpc_timeout_s", 20.0),
        max_retries=getattr(config, "max_retries", 3),
    )
    try:
        resolved = _resolve_pending_price_lots(
            state,
            rpc,
            wallet,
            config,
            decimals_by_mint,
            None,
        )
    finally:
        rpc.close()

    pending_after = count_pending(state)
    downgraded = pending_before - resolved - pending_after
    if downgraded < 0:
        downgraded = pending_before - resolved

    print(f"Resolved: {resolved}")
    print(f"Pending after: {pending_after}")
    print(f"Downgraded to unknown: {pending_before - resolved - pending_after if pending_before else 0}")

    save_state_atomic(state_path, state)
    print("State saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
