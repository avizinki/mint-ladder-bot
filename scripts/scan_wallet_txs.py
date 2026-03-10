#!/usr/bin/env python3
"""
Scan wallet transaction history for tracked mints. Produces a table of swaps/transfers
for CEO report: identify overnight sell and buy not reflected in state.

Usage (from mint-ladder-bot root, with RPC in .env):
  python scripts/scan_wallet_txs.py [--limit 500] [--state state.json] [--status status.json]
Output: CSV or table to stdout with tx_signature, timestamp, type, mint, amount_raw, sol_delta, program.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500, help="Max signatures to fetch")
    ap.add_argument("--state", type=Path, default=PROJECT_ROOT / "state.json", help="state.json path")
    ap.add_argument("--status", type=Path, default=PROJECT_ROOT / "status.json", help="status.json path")
    ap.add_argument("--format", choices=("table", "csv"), default="table")
    args = ap.parse_args()

    if not args.status.exists():
        print("status.json not found", file=sys.stderr)
        return 1
    status = json.loads(args.status.read_text())
    wallet = status.get("wallet")
    if not wallet:
        print("status.json has no wallet", file=sys.stderr)
        return 1
    mints = [m["mint"] for m in status.get("mints", [])]
    symbol_by_mint = {m["mint"]: (m.get("symbol") or m["mint"][:8]) for m in status.get("mints", [])}

    # Load config for RPC (uses RPC_ENDPOINT from env)
    try:
        from mint_ladder_bot.config import Config
        config = Config()
    except Exception as e:
        print(f"Config load failed: {e}", file=sys.stderr)
        return 1
    from mint_ladder_bot.rpc import RpcClient
    from mint_ladder_bot.tx_infer import (
        _parse_token_deltas_for_mints,
        _parse_sol_delta_lamports,
        _get_block_time,
        parse_sell_events_from_tx,
    )
    from mint_ladder_bot.tx_lot_engine import _parse_buy_events_from_tx

    rpc = RpcClient(config.rpc_endpoint, timeout_s=getattr(config, "rpc_timeout_s", 20.0), max_retries=getattr(config, "max_retries", 3))
    decimals_by_mint = {m["mint"]: m.get("decimals", 6) for m in status.get("mints", [])}
    mints_tracked = set(mints)

    try:
        sig_list = rpc.get_signatures_for_address(wallet, limit=args.limit)
    except Exception as e:
        print(f"get_signatures_for_address failed: {e}", file=sys.stderr)
        return 1

    rows: list[dict] = []
    for sig_info in sig_list:
        signature = sig_info.get("signature") if isinstance(sig_info, dict) else None
        if not signature:
            continue
        try:
            tx = rpc.get_transaction(signature)
        except Exception:
            continue
        if not tx:
            continue
        block_time = _get_block_time(tx)
        ts_str = block_time.isoformat().replace("+00:00", "Z") if block_time else ""
        sol_delta = _parse_sol_delta_lamports(tx, wallet)
        token_deltas = _parse_token_deltas_for_mints(tx, wallet, mints)
        sell_events = parse_sell_events_from_tx(tx, wallet, mints_tracked, signature)
        buy_events = _parse_buy_events_from_tx(tx, wallet, signature, mints_tracked, decimals_by_mint)
        if sell_events:
            for ev in sell_events:
                rows.append({
                    "tx_signature": signature,
                    "timestamp": ts_str,
                    "type": "sell",
                    "mint": ev.mint,
                    "symbol": symbol_by_mint.get(ev.mint, ev.mint[:8]),
                    "amount_raw": ev.sold_raw,
                    "sol_delta_lamports": ev.sol_in_lamports,
                    "program": "swap",
                })
        if buy_events:
            for ev in buy_events:
                rows.append({
                    "tx_signature": signature,
                    "timestamp": ts_str,
                    "type": "buy",
                    "mint": ev.mint,
                    "symbol": symbol_by_mint.get(ev.mint, ev.mint[:8]),
                    "amount_raw": ev.token_amount_raw,
                    "sol_delta_lamports": -ev.sol_spent_lamports if ev.sol_spent_lamports else 0,
                    "program": "swap",
                })
        if not sell_events and not buy_events and token_deltas:
            for mint, delta in token_deltas.items():
                if delta == 0:
                    continue
                rows.append({
                    "tx_signature": signature,
                    "timestamp": ts_str,
                    "type": "transfer_in" if delta > 0 else "transfer_out",
                    "mint": mint,
                    "symbol": symbol_by_mint.get(mint, mint[:8]),
                    "amount_raw": abs(delta),
                    "sol_delta_lamports": sol_delta or 0,
                    "program": "transfer",
                })

    rows.sort(key=lambda r: (r["timestamp"], r["tx_signature"]), reverse=True)

    if args.format == "csv":
        print("tx_signature,timestamp,type,mint,symbol,amount_raw,sol_delta_lamports,program")
        for r in rows:
            print(f"{r['tx_signature']},{r['timestamp']},{r['type']},{r['mint']},{r['symbol']},{r['amount_raw']},{r['sol_delta_lamports']},{r['program']}")
    else:
        print(f"Wallet: {wallet}")
        print(f"Tracked mints: {len(mints)} | Scanned {len(sig_list)} signatures | Matched {len(rows)} rows")
        print("-" * 120)
        for r in rows[:80]:
            print(f"{r['timestamp']} | {r['type']:10} | {r['symbol']:12} | amount_raw={r['amount_raw']} sol_delta={r['sol_delta_lamports']/1e9:.6f} | {r['tx_signature'][:20]}...")
        if len(rows) > 80:
            print(f"... and {len(rows) - 80} more rows")

    rpc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
