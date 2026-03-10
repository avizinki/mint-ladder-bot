#!/usr/bin/env python3
"""
One-off diagnostics for tx lookup: wallet + mint + expected delta.
Outputs: signatures scanned, slot range, each candidate tx (signature, slot, token_delta, sol_spent_lamports), whether any matched.
Use for lot_1757f48a full debug export. Loads .env and state.json for wallet; mint/delta passed as args.
Usage: from mint-ladder-bot: .venv/bin/python3 tools/debug_tx_lookup_lot.py DMYNp65mub3i7LRpBdB66CgBAceLcQnv4gsWeCi6pump 33819871399 [max_sigs=100]
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

from mint_ladder_bot.rpc import RpcClient
from mint_ladder_bot.tx_infer import (
    _parse_token_deltas_for_mints,
    _parse_sol_delta_lamports,
    _get_block_time,
)


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: debug_tx_lookup_lot.py <mint> <delta_raw> [max_sigs=100]", file=sys.stderr)
        return 1
    mint = sys.argv[1]
    try:
        delta_raw = int(sys.argv[2])
    except ValueError:
        print("delta_raw must be integer", file=sys.stderr)
        return 1
    max_sigs = int(sys.argv[3]) if len(sys.argv) > 3 else 100

    state_path = PROJECT_ROOT / "state.json"
    if not state_path.exists():
        print("state.json not found", file=sys.stderr)
        return 1
    state = json.loads(state_path.read_text())
    wallet = state.get("wallet")
    if not wallet:
        print("No wallet in state", file=sys.stderr)
        return 1

    from mint_ladder_bot.config import Config
    config = Config()
    rpc = RpcClient(
        config.rpc_endpoint,
        timeout_s=getattr(config, "rpc_timeout_s", 20.0),
        max_retries=getattr(config, "max_retries", 3),
    )

    try:
        sig_list = rpc.get_signatures_for_address(wallet, limit=max_sigs)
    except Exception as e:
        print(f"get_signatures_for_address error: {e}")
        return 1

    if not sig_list:
        print("signatures_scanned=0 slot_range=N/A (no signatures)")
        return 0

    slots = []
    candidates = []
    expected = delta_raw
    tolerance = max(1, int(delta_raw * 0.01))
    matched = None

    for sig_info in sig_list:
        sig = sig_info.get("signature") if isinstance(sig_info, dict) else None
        slot = sig_info.get("slot") if isinstance(sig_info, dict) else None
        if sig:
            slots.append(slot)
        if not sig:
            continue
        try:
            tx = rpc.get_transaction(sig)
        except Exception as e:
            candidates.append({
                "signature": sig,
                "slot": slot,
                "token_delta": None,
                "sol_spent_lamports": None,
                "error": str(e),
            })
            continue
        if not tx:
            candidates.append({"signature": sig, "slot": slot, "token_delta": None, "sol_spent_lamports": None, "error": "tx_null"})
            continue
        token_deltas = _parse_token_deltas_for_mints(tx, wallet, [mint])
        token_delta = token_deltas.get(mint, 0)
        sol_delta = _parse_sol_delta_lamports(tx, wallet)
        meta = tx.get("meta") or {}
        fee = int(meta.get("fee") or 0)
        sol_spent_lamports = (abs(sol_delta) - fee) if sol_delta is not None and sol_delta < 0 else None
        block_time = _get_block_time(tx)
        delta_matched = abs(token_delta - expected) <= tolerance if token_delta else False
        if delta_matched:
            matched = sig
        candidates.append({
            "signature": sig,
            "slot": slot,
            "block_time": block_time.isoformat() if block_time else None,
            "token_delta": token_delta,
            "sol_spent_lamports": sol_spent_lamports,
            "delta_matched_33819871399": delta_matched,
        })

    rpc.close()

    slot_min = min(s for s in slots if s is not None) if slots else None
    slot_max = max(s for s in slots if s is not None) if slots else None
    print("=== TX LOOKUP DIAGNOSTICS ===")
    print(f"wallet={wallet}")
    print(f"mint={mint}")
    print(f"expected_delta_raw={expected}")
    print(f"signatures_scanned={len(sig_list)}")
    print(f"slot_range=[{slot_min}, {slot_max}]")
    print(f"delta_matched={matched is not None} (signature={matched})")
    print()
    print("=== CANDIDATE TRANSACTIONS (token change for this mint) ===")
    for c in candidates:
        print(json.dumps(c, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
