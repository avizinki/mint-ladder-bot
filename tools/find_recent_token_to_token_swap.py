#!/usr/bin/env python3
"""
Find the most recent token→token swap for the runtime wallet.
Scans recent signatures and returns the first tx with negative delta on one mint
and positive delta on another (same wallet, same tx).
Usage: python tools/find_recent_token_to_token_swap.py [--limit 30] [--status path]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mint_ladder_bot.rpc import RpcClient
from mint_ladder_bot.tx_infer import (
    _parse_sol_delta_lamports,
    _parse_token_deltas_for_wallet_all_mints,
)
from mint_ladder_bot.tx_infer import _get_block_time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30, help="Max signatures to fetch")
    ap.add_argument("--status", type=Path, default=None, help="status.json path for wallet")
    ap.add_argument("--wallet", type=str, default=None, help="Wallet pubkey (overrides status)")
    ap.add_argument("--rpc", type=str, default=None, help="RPC URL (default: RPC_ENDPOINT from env)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    status_path = args.status or root / "status.json"
    wallet = args.wallet
    if not wallet and status_path.exists():
        with open(status_path) as f:
            data = json.load(f)
            wallet = data.get("wallet")
    if not wallet:
        print("Need --wallet or status.json with wallet", file=sys.stderr)
        return 1

    rpc_url = args.rpc or os.getenv("RPC_ENDPOINT", "").strip()
    if not rpc_url:
        print("Need RPC_ENDPOINT in env or --rpc", file=sys.stderr)
        return 1

    rpc = RpcClient(rpc_url, timeout_s=15.0)
    try:
        sig_list = rpc.get_signatures_for_address(wallet, limit=args.limit)
    except Exception as e:
        print(f"get_signatures_for_address failed: {e}", file=sys.stderr)
        return 1
    if not sig_list:
        print("No signatures returned", file=sys.stderr)
        return 1

    for sig_info in sig_list:
        sig = sig_info.get("signature") if isinstance(sig_info, dict) else None
        if not sig:
            continue
        try:
            tx = rpc.get_transaction(sig)
        except Exception as e:
            print(f"get_transaction {sig[:16]} failed: {e}", file=sys.stderr)
            continue
        if not tx or not tx.get("meta"):
            continue
        deltas = _parse_token_deltas_for_wallet_all_mints(tx, wallet)
        neg = [(m, d) for m, d in deltas.items() if d < 0]
        pos = [(m, d) for m, d in deltas.items() if d > 0]
        sol_delta = _parse_sol_delta_lamports(tx, wallet)
        sol_decrease = sol_delta is not None and sol_delta < 0
        if neg and pos:
            block_time = _get_block_time(tx)
            ts = block_time.isoformat() if block_time else "N/A"
            print(json.dumps({
                "signature": sig,
                "block_time": ts,
                "source_mints": [m for m, _ in neg],
                "source_deltas_raw": {m: d for m, d in neg},
                "destination_mints": [m for m, _ in pos],
                "destination_deltas_raw": {m: d for m, d in pos},
                "sol_decrease": sol_decrease,
                "classification": "token_to_token" if len(neg) == 1 and len(pos) >= 1 and not (sol_decrease and sol_delta and abs(sol_delta) > 5000) else "multi_hop_or_sol",
            }, indent=2))
            return 0
    print("No token→token swap found in recent txs", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
