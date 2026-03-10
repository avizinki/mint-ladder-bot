#!/usr/bin/env python3
"""
One-off: promote all display-pending lots to pending_price_resolution and run the resolver.
Display-pending = entry_confidence=snapshot and source != initial_migration (they show as pending in UI).
After run: each such lot is either resolved to tx_exact or downgraded to unknown (no more display-pending).

Run from project root: .venv/bin/python3 scripts/resolve_display_pending_lots.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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


def count_display_pending(state) -> int:
    n = 0
    for ms in getattr(state, "mints", {}).values():
        for lot in getattr(ms, "lots", None) or []:
            ec = getattr(lot, "entry_confidence", None)
            src = getattr(lot, "source", None)
            if ec == "snapshot" and src != "initial_migration":
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

    state = load_state(state_path, status_path)
    display_pending_before = count_display_pending(state)
    print(f"Display-pending lots before: {display_pending_before}")

    wallet = getattr(state, "wallet", None)
    if not wallet:
        print("No wallet in state", file=sys.stderr)
        return 1

    try:
        status_data = json.loads(status_path.read_text())
        mints_list = status_data.get("mints") or []
        decimals_by_mint = {m["mint"]: m.get("decimals", 6) for m in mints_list if isinstance(m, dict) and m.get("mint")}
    except Exception:
        decimals_by_mint = {}

    promoted = 0
    for mint, ms in getattr(state, "mints", {}).items():
        for lot in getattr(ms, "lots", None) or []:
            ec = getattr(lot, "entry_confidence", None)
            src = getattr(lot, "source", None)
            if ec != "snapshot" or src == "initial_migration":
                continue
            lot.entry_confidence = "pending_price_resolution"  # type: ignore[assignment]
            lot.entry_price_sol_per_token = None  # type: ignore[assignment]  # so resolver tries tx lookup
            promoted += 1
    print(f"Promoted to pending_price_resolution: {promoted}")

    config = Config()
    rpc = RpcClient(
        config.rpc_endpoint,
        timeout_s=getattr(config, "rpc_timeout_s", 20.0),
        max_retries=getattr(config, "max_retries", 3),
    )
    try:
        resolved = _resolve_pending_price_lots(state, rpc, wallet, config, decimals_by_mint, None)
        print(f"Resolver: resolved {resolved} to tx_exact")
    finally:
        rpc.close()

    display_pending_after = count_display_pending(state)
    downgraded = display_pending_before - resolved - display_pending_after
    print(f"Display-pending lots after: {display_pending_after}")
    print(f"Resolved to tx_exact: {resolved}")
    print(f"Downgraded to unknown: {downgraded}")

    save_state_atomic(state_path, state)
    print("State saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
