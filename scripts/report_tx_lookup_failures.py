#!/usr/bin/env python3
"""
TX lookup failure report for pending_price_resolution lots.
For each such lot, attempts find_buy_tx_for_delta and records outcome:
  resolved, tx_not_found, delta_mismatch, scan_window_exceeded, rpc_error.
Read-only: does not mutate state.
Run from project root: .venv/bin/python3 scripts/report_tx_lookup_failures.py
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
from mint_ladder_bot.state import load_state
from mint_ladder_bot.rpc import RpcClient
from mint_ladder_bot.tx_infer import find_buy_tx_for_delta


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

    pending: list[tuple[str, str, str, int]] = []
    for mint, ms in getattr(state, "mints", {}).items():
        for lot in getattr(ms, "lots", None) or []:
            if getattr(lot, "entry_confidence", None) != "pending_price_resolution":
                continue
            try:
                raw = int(getattr(lot, "token_amount", 0) or 0)
            except (ValueError, TypeError):
                continue
            if raw <= 0:
                continue
            lot_id = (getattr(lot, "lot_id", None) or "")[:8]
            pending.append((mint, lot_id, mint[:12], raw))

    if not pending:
        print("No pending_price_resolution lots in state.")
        return 0

    rpc = RpcClient(
        config.rpc_endpoint,
        timeout_s=getattr(config, "rpc_timeout_s", 20.0),
        max_retries=getattr(config, "max_retries", 3),
    )
    breakdown: dict[str, list[tuple[str, str]]] = {
        "resolved": [],
        "tx_not_found": [],
        "delta_mismatch": [],
        "scan_window_exceeded": [],
        "rpc_error": [],
    }
    existing_sigs: set[str] = set()

    try:
        for mint, lot_id, mint_short, delta_raw in pending:
            reason_list: list[str] = []
            dec = decimals_by_mint.get(mint, 6)
            result = find_buy_tx_for_delta(
                wallet,
                mint,
                delta_raw,
                rpc,
                max_signatures=getattr(config, "entry_scan_max_signatures", 30),
                exclude_signatures=existing_sigs,
                decimals=dec,
                failure_reason_out=reason_list,
            )
            if result:
                breakdown["resolved"].append((mint_short, lot_id))
                existing_sigs.add(result[0])
            else:
                reason = reason_list[0] if reason_list else "tx_not_found"
                if reason not in breakdown:
                    breakdown[reason] = []
                breakdown[reason].append((mint_short, lot_id))
    finally:
        rpc.close()

    print("=== TX lookup failure report (pending_price_resolution lots) ===\n")
    for key in ["resolved", "tx_not_found", "delta_mismatch", "scan_window_exceeded", "rpc_error"]:
        items = breakdown.get(key, [])
        print(f"{key}: {len(items)}")
        for mint_short, lot_id in items[:20]:
            print(f"  mint={mint_short} lot_id={lot_id}")
        if len(items) > 20:
            print(f"  ... and {len(items) - 20} more")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
