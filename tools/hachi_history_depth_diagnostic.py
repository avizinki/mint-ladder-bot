#!/usr/bin/env python3
"""
CEO Command — Verify lot engine history depth for $HACHI.
Read-only: load state, find earliest lot, find earliest $HACHI tx in wallet history, compare.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from datetime import datetime, timezone

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

def _load_env():
    for p in (_REPO / ".env", Path(".env")):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()

_load_env()

MINT = "x95HN3DWvbfCBtTjGm587z8suK3ec6cwQwgZNLbWKyp"
WALLET = "3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR"
STATE_PATH = _REPO / "runtime" / "projects" / "mint_ladder_bot" / "state.json"


def main() -> None:
    import json
    from mint_ladder_bot.config import Config
    from mint_ladder_bot.rpc import RpcClient
    from mint_ladder_bot.tx_infer import _parse_token_deltas_for_mints, _get_block_time

    config = Config()
    state = json.load(STATE_PATH.open())
    ms = state.get("mints", {}).get(MINT)
    if not ms:
        print("Mint not found in state")
        return

    lots = ms.get("lots", [])
    def ts(lot):
        for k in ("detected_at", "created_at"):
            v = lot.get(k)
            if not v:
                continue
            if isinstance(v, str):
                try:
                    return datetime.fromisoformat(v.replace("Z", "+00:00"))
                except Exception:
                    return datetime.max.replace(tzinfo=timezone.utc)
            return v
        return datetime.max.replace(tzinfo=timezone.utc)

    sorted_lots = sorted(lots, key=ts)
    earliest_lot = sorted_lots[0] if sorted_lots else None
    if not earliest_lot:
        print("No lots")
        return

    engine_start = earliest_lot.get("detected_at") or earliest_lot.get("created_at")
    try:
        engine_start_dt = datetime.fromisoformat(engine_start.replace("Z", "+00:00"))
    except Exception:
        engine_start_dt = None

    print("=" * 60)
    print("1️⃣ EARLIEST LOT CREATED BY ENGINE")
    print("=" * 60)
    print("lot_id:", earliest_lot.get("lot_id"))
    print("detected_at:", engine_start)
    print("tx_signature:", earliest_lot.get("tx_signature"))
    print()

    # Fetch wallet signature list (paginate to get older txs)
    rpc = RpcClient(config.rpc_endpoint, timeout_s=getattr(config, "rpc_timeout_s", 20.0), max_retries=config.max_retries)
    sig_list = []
    before = None
    for _ in range(8):  # up to 8000 sigs
        batch = rpc.get_signatures_for_address(WALLET, limit=1000, before=before)
        if not batch:
            break
        sig_list.extend(batch)
        if len(sig_list) >= 721:
            break
        before = batch[-1].get("signature") if isinstance(batch[-1], dict) else None
        if not before:
            break

    # Walk from oldest (end of list) and find first tx with $HACHI delta
    earliest_hachi_ts = None
    earliest_hachi_sig = None
    earliest_hachi_delta = None
    # Check from the end (oldest) - sample every N to avoid too many getTransaction
    step = max(1, len(sig_list) // 80)  # ~80 txs max
    for i in range(len(sig_list) - 1, -1, -step):
        sig_info = sig_list[i]
        sig = sig_info.get("signature") if isinstance(sig_info, dict) else None
        if not sig:
            continue
        try:
            tx = rpc.get_transaction(sig)
        except Exception:
            continue
        if not tx:
            continue
        deltas = _parse_token_deltas_for_mints(tx, WALLET, [MINT])
        delta_raw = deltas.get(MINT, 0)
        if delta_raw == 0:
            continue
        ts_val = _get_block_time(tx)
        if ts_val and (earliest_hachi_ts is None or ts_val < earliest_hachi_ts):
            earliest_hachi_ts = ts_val
            earliest_hachi_sig = sig
            earliest_hachi_delta = delta_raw
        # Once we have one from the old end, we can break or refine; we want the absolute earliest
        # so keep going backward (we're iterating from end with step; for finer grain we'd re-scan)
    # Refine: if we found one, scan backward from that index with step=1 to get true earliest
    if earliest_hachi_sig and earliest_hachi_ts:
        idx = next((i for i, s in enumerate(sig_list) if (s.get("signature") if isinstance(s, dict) else None) == earliest_hachi_sig), None)
        if idx is not None:
            # Refine: check up to 60 older txs (higher index = older)
            for j in range(idx + 1, min(idx + 61, len(sig_list))):
                sig_info = sig_list[j]
                sig = sig_info.get("signature") if isinstance(sig_info, dict) else None
                if not sig:
                    continue
                try:
                    tx = rpc.get_transaction(sig)
                except Exception:
                    continue
                if not tx:
                    continue
                deltas = _parse_token_deltas_for_mints(tx, WALLET, [MINT])
                delta_raw = deltas.get(MINT, 0)
                if delta_raw == 0:
                    continue
                ts_val = _get_block_time(tx)
                if ts_val and ts_val < earliest_hachi_ts:
                    earliest_hachi_ts = ts_val
                    earliest_hachi_sig = sig
                    earliest_hachi_delta = delta_raw
    rpc.close()

    print("2️⃣ EARLIEST REAL $HACHI TRANSACTION IN WALLET HISTORY")
    print("=" * 60)
    if earliest_hachi_ts:
        print("earliest_hachi_tx_timestamp:", earliest_hachi_ts.isoformat())
        print("earliest_hachi_tx_signature:", earliest_hachi_sig)
        print("delta_raw:", earliest_hachi_delta)
    else:
        print("Could not determine (no $HACHI delta in sampled txs)")
    print()

    print("3️⃣ HISTORY GAP")
    print("=" * 60)
    if engine_start_dt and earliest_hachi_ts:
        gap = (engine_start_dt - earliest_hachi_ts).total_seconds()
        print("engine_history_start (earliest_lot_detected_at):", engine_start)
        print("actual_history_start (earliest_hachi_tx_timestamp):", earliest_hachi_ts.isoformat())
        print("history_gap_seconds:", gap)
        print("history_gap_hours:", round(gap / 3600, 2))
        print("history_gap_days:", round(gap / 86400, 2))
    else:
        print("Cannot compute (missing engine_start or earliest_hachi_ts)")
    print()

    print("4️⃣ LIMITS USED BY LOT ENGINE")
    print("=" * 60)
    # From code: tx-backfill uses max(100, min(signatures, 3000)); we ran with 3000.
    # Runner startup uses ENTRY_SCAN_MAX_SIGNATURES (300) or 60; tx_lot_engine page_limit 1000.
    from mint_ladder_bot.runner import ENTRY_SCAN_MAX_SIGNATURES
    recon = getattr(config, "reconstruction_max_signatures_per_wallet", 500)
    entry_limit = getattr(config, "entry_infer_signature_limit", 60)
    print("max_signatures_used (tx-backfill rebuild): 3000 (from CLI --signatures 3000)")
    print("backfill_limit_used (TX_BACKFILL_ONCE in runner): min(max(100,TX_BACKFILL_SIGNATURES),500) -> max 500")
    print("rpc_page_size (tx_lot_engine): 1000")
    print("ENTRY_SCAN_MAX_SIGNATURES (runner):", ENTRY_SCAN_MAX_SIGNATURES)
    print("reconstruction_max_signatures_per_wallet (config):", recon)
    print("entry_infer_signature_limit (config):", entry_limit)
    print()

    print("5️⃣ CONCLUSION")
    print("=" * 60)
    if engine_start_dt and earliest_hachi_ts:
        if earliest_hachi_ts >= engine_start_dt:
            conclusion = "A) history fully covered"
        else:
            gap_days = (engine_start_dt - earliest_hachi_ts).total_seconds() / 86400
            if gap_days > 0:
                conclusion = "B) history partially covered (scan window limit)"
            else:
                conclusion = "A) history fully covered"
    else:
        conclusion = "D) other (could not determine earliest $HACHI tx)"
    print(conclusion)


if __name__ == "__main__":
    main()
