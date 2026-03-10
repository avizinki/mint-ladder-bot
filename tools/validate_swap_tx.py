#!/usr/bin/env python3
"""
Validate a single swap tx: classification, source/destination, valuation, state/status/dashboard.
Regression test for token→token swap handling.
Usage: python tools/validate_swap_tx.py --sig <signature> [--state path] [--status path] [--dashboard-url url]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mint_ladder_bot.rpc import RpcClient
from mint_ladder_bot.state import load_state
from mint_ladder_bot.tx_infer import (
    _get_block_time,
    _parse_sol_delta_lamports,
    _parse_token_deltas_for_wallet_all_mints,
)
from mint_ladder_bot.tx_lot_engine import (
    _parse_buy_events_from_tx,
    _source_cost_basis_sol,
    run_tx_first_lot_engine,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sig", required=True, help="Transaction signature")
    ap.add_argument("--state", type=Path, default=None)
    ap.add_argument("--status", type=Path, default=None)
    ap.add_argument("--dashboard-url", type=str, default="http://127.0.0.1:6200")
    ap.add_argument("--reprocess", action="store_true", help="Re-run tx-first with small window and save state")
    ap.add_argument("--reprocess-max-sigs", type=int, default=20, help="When --reprocess, fetch this many recent sigs (default 20)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    state_path = args.state or root / "state.json"
    status_path = args.status or root / "status.json"

    if not status_path.exists():
        print("status.json not found", file=sys.stderr)
        return 1
    status_data = json.loads(status_path.read_text())
    wallet = status_data.get("wallet")
    if not wallet:
        print("status.json has no wallet", file=sys.stderr)
        return 1

    rpc_url = os.getenv("RPC_ENDPOINT", "").strip()
    if not rpc_url:
        print("RPC_ENDPOINT not set", file=sys.stderr)
        return 1

    rpc = RpcClient(rpc_url, timeout_s=15.0)
    try:
        tx = rpc.get_transaction(args.sig)
    except Exception as e:
        print(f"get_transaction failed: {e}", file=sys.stderr)
        return 1
    if not tx:
        print("Tx not found or null", file=sys.stderr)
        return 1

    # Load state for mints_tracked and decimals
    state = load_state(state_path, status_path)
    decimals_by_mint = {m.get("mint", ""): int(m.get("decimals", 6)) for m in status_data.get("mints", [])}
    for mint in state.mints:
        if mint not in decimals_by_mint:
            decimals_by_mint[mint] = 6
    mints_tracked = set(state.mints.keys())

    # Parse
    deltas = _parse_token_deltas_for_wallet_all_mints(tx, wallet)
    neg = [(m, d) for m, d in deltas.items() if d < 0]
    pos = [(m, d) for m, d in deltas.items() if d > 0]
    sol_delta = _parse_sol_delta_lamports(tx, wallet)
    block_time = _get_block_time(tx)
    meta = tx.get("meta") or {}
    fee = int(meta.get("fee") or 0)

    # Classification
    sol_decrease = sol_delta is not None and sol_delta < 0
    num_inputs = len(neg) + (1 if sol_decrease and sol_delta and abs(sol_delta) - fee > 0 else 0)
    swap_type = "multi_hop" if num_inputs > 1 else "token_to_token" if (neg and pos) else "unknown"

    result = {
        "signature": args.sig,
        "block_time": block_time.isoformat() if block_time else None,
        "wallet": wallet,
        "classification": swap_type,
        "source_side": [{"mint": m, "delta_raw": d, "delta_ui": d / (10 ** decimals_by_mint.get(m, 6))} for m, d in neg],
        "destination_side": [{"mint": m, "delta_raw": d, "delta_ui": d / (10 ** decimals_by_mint.get(m, 6))} for m, d in pos],
        "sol_delta_lamports": sol_delta,
    }

    # Buy events from tx_lot_engine (only includes mints in mints_tracked)
    events = _parse_buy_events_from_tx(tx, wallet, args.sig, mints_tracked, decimals_by_mint)
    if events:
        # Enrich token→token with source cost basis (same as tx_lot_engine)
        for ev in events:
            if ev.swap_type in ("token_to_token", "multi_hop") and ev.entry_price_sol_per_token is None and ev.input_asset_mint and ev.input_amount_raw:
                res = _source_cost_basis_sol(state, ev.input_asset_mint, ev.input_amount_raw, decimals_by_mint)
                if res is not None:
                    cost_sol, method = res
                    dec = decimals_by_mint.get(ev.mint, 6)
                    token_human = ev.token_amount_raw / (10 ** dec)
                    if token_human > 0:
                        from mint_ladder_bot.tx_lot_engine import _validate_entry_price
                        ev.entry_price_sol_per_token = cost_sol / token_human
                        if _validate_entry_price(ev.entry_price_sol_per_token):
                            ev.confidence = "inferred"
                            ev.valuation_method = method
        result["parsed_events"] = [
            {
                "mint": e.mint,
                "token_amount_raw": e.token_amount_raw,
                "entry_price_sol_per_token": e.entry_price_sol_per_token,
                "swap_type": e.swap_type,
                "valuation_method": getattr(e, "valuation_method", None),
                "input_asset_mint": e.input_asset_mint,
                "input_amount_raw": e.input_amount_raw,
                "source_sold_raw": getattr(e, "source_sold_raw", None),
            }
            for e in events
        ]
    else:
        result["parsed_events"] = []

    # State check: destination lot with this sig?
    dest_mints = [e["mint"] for e in result["destination_side"]]
    source_mints = [e["mint"] for e in result["source_side"]]
    result["state"] = {}
    for mint in dest_mints:
        ms = state.mints.get(mint)
        if not ms:
            result["state"][mint] = "mint_not_in_state"
            continue
        lots = getattr(ms, "lots", None) or []
        lot_for_sig = next((l for l in lots if getattr(l, "tx_signature", None) == args.sig), None)
        if lot_for_sig:
            result["state"][mint] = {
                "destination_lot_created": True,
                "entry_price_sol_per_token": getattr(lot_for_sig, "entry_price_sol_per_token", None),
                "swap_type": getattr(lot_for_sig, "swap_type", None),
                "valuation_method": getattr(lot_for_sig, "valuation_method", None),
            }
            result["state"][mint]["mint_entry_price_sol_per_token"] = getattr(ms, "entry_price_sol_per_token", None)
        else:
            result["state"][mint] = {"destination_lot_created": False}

    for mint in source_mints:
        ms = state.mints.get(mint)
        if not ms:
            result["state"][mint] = result["state"].get(mint) or "mint_not_in_state"
            continue
        lots = getattr(ms, "lots", None) or []
        total_remaining = sum(int(getattr(l, "remaining_amount", 0) or 0) for l in lots if getattr(l, "status", "active") == "active")
        result["state"][mint] = result["state"].get(mint) or {}
        if isinstance(result["state"][mint], dict):
            result["state"][mint]["source_trading_bag_raw"] = total_remaining
            result["state"][mint]["source_lots_count"] = len(lots)
            sold_in_tx = next((getattr(e, "source_sold_raw", None) for e in events if getattr(e, "input_asset_mint", None) == mint), None)
            result["state"][mint]["expected_debit_raw"] = sold_in_tx
            # Source accounting: pre = total_remaining + sold_in_tx (post + debited)
            if sold_in_tx is not None:
                result["state"][mint]["source_pre_remaining_raw"] = total_remaining + sold_in_tx
                result["state"][mint]["source_debited_raw"] = sold_in_tx
                result["state"][mint]["source_post_remaining_raw"] = total_remaining
                result["state"][mint]["source_debit_correct"] = (total_remaining + sold_in_tx - sold_in_tx == total_remaining)

    # Status check: entry for destination mint
    status_mints = {m.get("mint"): m for m in status_data.get("mints", [])}
    result["status"] = {}
    for mint in dest_mints:
        sm = status_mints.get(mint)
        if not sm:
            result["status"][mint] = "mint_not_in_status"
        else:
            entry = (sm.get("entry") or {}).get("entry_price_sol_per_token")
            result["status"][mint] = {"entry_price_sol_per_token": entry, "reflects_entry": entry is not None and float(entry or 0) > 0}

    # Dashboard: fetch if url given
    result["dashboard"] = {}
    if args.dashboard_url:
        try:
            import urllib.request
            req = urllib.request.Request(args.dashboard_url.rstrip("/") + "/runtime/dashboard", headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=10) as r:
                dash = json.loads(r.read().decode())
        except Exception as e:
            result["dashboard"]["error"] = str(e)
        else:
            positions = (dash.get("positions") or [])
            for mint in dest_mints:
                row = next((p for p in positions if p.get("mint") == mint), None)
                if not row:
                    result["dashboard"][mint] = "not_in_dashboard"
                else:
                    ep = row.get("entry_sol_per_token")
                    result["dashboard"][mint] = {
                        "entry_sol_per_token": ep,
                        "no_longer_na": ep is not None and (ep if isinstance(ep, (int, float)) else 0) > 0,
                    }

    # Reprocess if requested
    if args.reprocess and state_path.exists():
        from mint_ladder_bot.state import save_state_atomic
        n = run_tx_first_lot_engine(
            state, rpc, wallet,
            decimals_by_mint,
            journal_path=None,
            max_signatures=args.reprocess_max_sigs,
            symbol_by_mint={m.get("mint", ""): (m.get("symbol") or (m.get("mint") or "")[:8]) for m in status_data.get("mints", [])},
        )
        save_state_atomic(state_path, state)
        result["reprocess"] = {"lots_created": n, "state_saved": True}
        # Re-check state after reprocess
        for mint in dest_mints:
            ms = state.mints.get(mint)
            if ms:
                lots = getattr(ms, "lots", None) or []
                lot_for_sig = next((l for l in lots if getattr(l, "tx_signature", None) == args.sig), None)
                if lot_for_sig:
                    result["state"][mint] = {
                        "destination_lot_created": True,
                        "entry_price_sol_per_token": getattr(lot_for_sig, "entry_price_sol_per_token", None),
                        "swap_type": getattr(lot_for_sig, "swap_type", None),
                        "valuation_method": getattr(lot_for_sig, "valuation_method", None),
                        "acquired_via_swap": getattr(lot_for_sig, "acquired_via_swap", False),
                    }
        for mint in source_mints:
            ms = state.mints.get(mint)
            if ms and isinstance(result["state"].get(mint), dict):
                result["state"][mint]["source_trading_bag_raw"] = sum(
                    int(getattr(l, "remaining_amount", 0) or 0)
                    for l in (getattr(ms, "lots", None) or [])
                    if getattr(l, "status", "active") == "active"
                )

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
