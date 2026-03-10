#!/usr/bin/env python3
"""
Deep diagnosis for lot 1757f48a: scan wallet + token-account scopes (500 sigs each),
time-bound 30 min before/after creation, multi-tx delta reconstruction.
Outputs: address-scope comparison, in-window candidates, single-tx vs multi-tx match.
Run from mint-ladder-bot with .env loaded. Writes results to stdout as JSON for doc inclusion.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
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

# Lot 1757f48a constants
LOT_CREATION_UTC = "2026-03-07T22:47:09.841255Z"
WINDOW_MINS = 30
EXPECTED_DELTA_RAW = 33_819_871_399
MINT = "DMYNp65mub3i7LRpBdB66CgBAceLcQnv4gsWeCi6pump"


def parse_creation_ts(s: str):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def run_scope(
    rpc: RpcClient,
    scope_name: str,
    address: str,
    wallet: str,
    mint: str,
    window_start: float,
    window_end: float,
    delay_s: float = 0.12,
) -> dict:
    """Fetch up to 500 sigs for address, filter by blockTime in window, fetch txs, parse token/SOL deltas."""
    try:
        sig_list = rpc.get_signatures_for_address(address, limit=500)
    except Exception as e:
        return {
            "scope": scope_name,
            "address": address,
            "error": str(e),
            "signatures_scanned": 0,
            "slot_range": None,
            "in_window_count": 0,
            "candidates": [],
            "sum_positive_delta": 0,
            "single_tx_match": False,
            "multi_tx_sum_match": False,
        }
    if not sig_list:
        return {
            "scope": scope_name,
            "address": address,
            "signatures_scanned": 0,
            "slot_range": None,
            "in_window_count": 0,
            "candidates": [],
            "sum_positive_delta": 0,
            "single_tx_match": False,
            "multi_tx_sum_match": False,
        }
    slots = [s.get("slot") for s in sig_list if s.get("slot") is not None]
    slot_range = [min(slots), max(slots)] if slots else None

    # Filter sigs by blockTime in window (blockTime can be null on some RPCs)
    in_window = []
    for s in sig_list:
        bt = s.get("blockTime")
        if bt is not None and window_start <= bt <= window_end:
            in_window.append(s)

    candidates = []
    for sig_info in in_window:
        sig = sig_info.get("signature")
        slot = sig_info.get("slot")
        block_ts = sig_info.get("blockTime")
        if not sig:
            continue
        time.sleep(delay_s)
        try:
            tx = rpc.get_transaction(sig)
        except Exception as e:
            candidates.append({
                "signature": sig,
                "slot": slot,
                "block_time": block_ts,
                "token_delta": None,
                "sol_spent_lamports": None,
                "error": str(e)[:200],
            })
            continue
        if not tx:
            candidates.append({
                "signature": sig,
                "slot": slot,
                "block_time": block_ts,
                "token_delta": None,
                "sol_spent_lamports": None,
                "error": "tx_null",
            })
            continue
        token_deltas = _parse_token_deltas_for_mints(tx, wallet, [mint])
        token_delta = token_deltas.get(mint, 0)
        sol_delta = _parse_sol_delta_lamports(tx, wallet)
        meta = tx.get("meta") or {}
        fee = int(meta.get("fee") or 0)
        sol_spent = (abs(sol_delta) - fee) if sol_delta is not None and sol_delta < 0 else None
        bt = _get_block_time(tx)
        candidates.append({
            "signature": sig,
            "slot": slot,
            "block_time": bt.timestamp() if bt else block_ts,
            "block_time_iso": bt.isoformat() if bt else None,
            "token_delta": token_delta,
            "sol_spent_lamports": sol_spent,
        })
    tolerance = max(1, int(EXPECTED_DELTA_RAW * 0.01))
    sum_positive = sum(c.get("token_delta") or 0 for c in candidates if (c.get("token_delta") or 0) > 0)
    single_match = any(
        c.get("token_delta") is not None
        and abs((c.get("token_delta") or 0) - EXPECTED_DELTA_RAW) <= tolerance
        for c in candidates
    )
    sum_match = abs(sum_positive - EXPECTED_DELTA_RAW) <= tolerance if sum_positive else False

    return {
        "scope": scope_name,
        "address": address,
        "signatures_scanned": len(sig_list),
        "slot_range": slot_range,
        "in_window_count": len(in_window),
        "candidates": candidates,
        "sum_positive_delta": sum_positive,
        "single_tx_match": single_match,
        "multi_tx_sum_match": sum_match,
    }


def main() -> int:
    state_path = PROJECT_ROOT / "state.json"
    status_path = PROJECT_ROOT / "status.json"
    if not state_path.exists() or not status_path.exists():
        print("state.json or status.json not found", file=sys.stderr)
        return 1
    state = json.loads(state_path.read_text())
    status = json.loads(status_path.read_text())
    wallet = state.get("wallet")
    if not wallet:
        print("No wallet in state", file=sys.stderr)
        return 1
    token_account = None
    for m in (status.get("mints") or []):
        if isinstance(m, dict) and m.get("mint") == MINT:
            token_account = m.get("token_account")
            break
    if not token_account:
        print("Token account for mint not found in status.json", file=sys.stderr)
        return 1

    creation = parse_creation_ts(LOT_CREATION_UTC)
    window_start = creation.timestamp() - WINDOW_MINS * 60
    window_end = creation.timestamp() + WINDOW_MINS * 60

    from mint_ladder_bot.config import Config
    config = Config()
    rpc = RpcClient(
        config.rpc_endpoint,
        timeout_s=getattr(config, "rpc_timeout_s", 25.0),
        max_retries=getattr(config, "max_retries", 3),
    )

    out = {
        "lot_id": "1757f48a",
        "mint": MINT,
        "wallet": wallet,
        "token_account": token_account,
        "creation_utc": LOT_CREATION_UTC,
        "window_start_utc": datetime.fromtimestamp(window_start, tz=timezone.utc).isoformat(),
        "window_end_utc": datetime.fromtimestamp(window_end, tz=timezone.utc).isoformat(),
        "expected_delta_raw": EXPECTED_DELTA_RAW,
        "scopes": [],
    }

    # Scope A: wallet
    out["scopes"].append(
        run_scope(rpc, "wallet", wallet, wallet, MINT, window_start, window_end)
    )
    # Scope B: token account
    ta_result = run_scope(rpc, "token_account", token_account, wallet, MINT, window_start, window_end)
    out["scopes"].append(ta_result)
    # Fallback: if token_account had 0 in window, fetch first 15 sigs by slot and parse (in case blockTime was null)
    if ta_result.get("in_window_count") == 0 and ta_result.get("signatures_scanned", 0) > 0:
        sig_list_ta = rpc.get_signatures_for_address(token_account, limit=20)
        fallback_candidates = []
        for sig_info in sig_list_ta[:15]:
            sig = sig_info.get("signature")
            if not sig:
                continue
            time.sleep(0.12)
            try:
                tx = rpc.get_transaction(sig)
            except Exception:
                continue
            if not tx:
                continue
            token_deltas = _parse_token_deltas_for_mints(tx, wallet, [MINT])
            token_delta = token_deltas.get(MINT, 0)
            sol_delta = _parse_sol_delta_lamports(tx, wallet)
            meta = tx.get("meta") or {}
            fee = int(meta.get("fee") or 0)
            sol_spent = (abs(sol_delta) - fee) if sol_delta is not None and sol_delta < 0 else None
            bt = _get_block_time(tx)
            fallback_candidates.append({
                "signature": sig,
                "slot": sig_info.get("slot"),
                "block_time_iso": bt.isoformat() if bt else None,
                "token_delta": token_delta,
                "sol_spent_lamports": sol_spent,
            })
        ta_result["fallback_first_15"] = fallback_candidates
        tolerance = max(1, int(EXPECTED_DELTA_RAW * 0.01))
        single = any(
            c.get("token_delta") is not None and abs((c.get("token_delta") or 0) - EXPECTED_DELTA_RAW) <= tolerance
            for c in fallback_candidates
        )
        ssum = sum(c.get("token_delta") or 0 for c in fallback_candidates if (c.get("token_delta") or 0) > 0)
        ta_result["fallback_single_tx_match"] = single
        ta_result["fallback_sum_positive"] = ssum
        ta_result["fallback_multi_tx_sum_match"] = abs(ssum - EXPECTED_DELTA_RAW) <= tolerance
    rpc.close()

    # Summary for classification
    wallet_scope = next((s for s in out["scopes"] if s["scope"] == "wallet"), {})
    ta_scope = next((s for s in out["scopes"] if s["scope"] == "token_account"), {})
    out["summary"] = {
        "wallet_in_window_count": wallet_scope.get("in_window_count", 0),
        "token_account_in_window_count": ta_scope.get("in_window_count", 0),
        "wallet_single_tx_match": wallet_scope.get("single_tx_match", False),
        "token_account_single_tx_match": ta_scope.get("single_tx_match", False),
        "wallet_sum_positive": wallet_scope.get("sum_positive_delta", 0),
        "token_account_sum_positive": ta_scope.get("sum_positive_delta", 0),
        "wallet_multi_tx_sum_match": wallet_scope.get("multi_tx_sum_match", False),
        "token_account_multi_tx_sum_match": ta_scope.get("multi_tx_sum_match", False),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
