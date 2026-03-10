#!/usr/bin/env python3
"""
CEO Investigation — Full reconciliation_mismatch diagnostic for $HACHI.
Read-only: loads runtime data, traces deltas, runs engine in memory. No fixes, no code changes.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

# Repo / project layout
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Load .env
def _load_env():
    for p in (_REPO / ".env", Path(".env")):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and k not in os.environ:
                os.environ[k] = v

_load_env()

MINT = "x95HN3DWvbfCBtTjGm587z8suK3ec6cwQwgZNLbWKyp"
SYMBOL = "$HACHI"
WALLET = "3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR"
DATA_DIR = _REPO / "runtime" / "projects" / "mint_ladder_bot"
STATE_PATH = DATA_DIR / "state.json"
STATUS_PATH = DATA_DIR / "status.json"
EVENTS_PATH = DATA_DIR / "events.jsonl"


def main() -> None:
    from mint_ladder_bot.state import load_state
    from mint_ladder_bot.models import StatusFile, RuntimeState, RuntimeMintState
    from mint_ladder_bot.reconciliation_report import compute_reconciliation_records
    from mint_ladder_bot.tx_infer import (
        _parse_token_deltas_for_mints,
        _get_block_time,
        _parse_sol_delta_lamports,
    )
    from mint_ladder_bot.config import Config
    from mint_ladder_bot.tx_lot_engine import run_tx_first_lot_engine
    from mint_ladder_bot.rpc import RpcClient

    config = Config()
    state = load_state(STATE_PATH, STATUS_PATH)
    status = StatusFile.model_validate_json(STATUS_PATH.read_text())

    # ----- Step 1: Load runtime data -----
    ms = state.mints.get(MINT)
    if not ms:
        print("ERROR: Mint not in state")
        return
    status_mint = next((m for m in status.mints if m.mint == MINT), None)
    wallet_balance_raw = int(getattr(status_mint, "balance_raw", 0) or 0) if status_mint else 0
    decimals = int(getattr(status_mint, "decimals", 6) or 6) if status_mint else 6

    # ----- Step 2: Core reconciliation numbers -----
    records = compute_reconciliation_records(state, status, mint_filter=MINT)
    rec = records[0] if records else None
    if not rec:
        print("ERROR: No reconciliation record for mint")
        return

    lots = getattr(ms, "lots", None) or []
    active_lots = [l for l in lots if getattr(l, "status", "active") == "active"]
    sum_active_raw = sum(int(getattr(l, "remaining_amount", 0) or 0) for l in active_lots)
    sum_all_raw = sum(int(getattr(l, "remaining_amount", 0) or 0) for l in lots)
    runtime_tradable_raw = int(ms.trading_bag_raw or 0) if hasattr(ms, "trading_bag_raw") else sum_active_raw
    diff_raw = wallet_balance_raw - sum_active_raw
    diff_pct = (diff_raw / wallet_balance_raw * 100) if wallet_balance_raw else None

    def _ui(raw: int) -> float:
        return raw / (10 ** decimals) if decimals else raw

    report_lines = []
    def out(s: str = "") -> None:
        report_lines.append(s)
        print(s)

    out("=" * 60)
    out("1️⃣ CORE RECONCILIATION NUMBERS ($HACHI)")
    out("=" * 60)
    out(f"wallet_balance_raw     = {wallet_balance_raw}")
    out(f"wallet_balance_ui      = {_ui(wallet_balance_raw)}")
    out(f"sum_active_lots_raw    = {sum_active_raw}")
    out(f"sum_active_lots_ui     = {_ui(sum_active_raw)}")
    out(f"sum_all_lots_raw       = {sum_all_raw}")
    out(f"sum_all_lots_ui        = {_ui(sum_all_raw)}")
    out(f"runtime_tradable_raw   = {runtime_tradable_raw}")
    out(f"diff_raw               = {diff_raw}")
    out(f"diff_pct               = {diff_pct}%" if diff_pct is not None else "diff_pct = N/A")
    out(f"reconciliation_status  = {rec.reconciliation_status}")
    out(f"blocker_category      = {rec.blocker_category}")
    out()

    # ----- Step 3: Lot breakdown -----
    out("2️⃣ LOT BREAKDOWN")
    out("-" * 60)
    out(f"{'lot_id':<12} {'source':<14} {'status':<10} {'token_amount':>16} {'remaining_amount':>18} {'entry_price':>14} {'detected_at':<28} tx_signature")
    sum_remaining_active = 0
    for i, lot in enumerate(lots):
        lot_id = getattr(lot, "lot_id", None) or getattr(lot, "tx_signature", "")[:12] or f"lot_{i}"
        src = getattr(lot, "source", "unknown") or "unknown"
        status_val = getattr(lot, "status", "active") or "active"
        token_amt = int(getattr(lot, "token_amount", 0) or 0)
        rem = int(getattr(lot, "remaining_amount", 0) or 0)
        if status_val == "active":
            sum_remaining_active += rem
        ep = getattr(lot, "entry_price_sol_per_token", None)
        ep_s = f"{ep:.2e}" if ep is not None else "N/A"
        det = getattr(lot, "detected_at", None) or getattr(lot, "created_at", "") or ""
        if hasattr(det, "isoformat"):
            det = det.isoformat()[:26]
        sig = getattr(lot, "tx_signature", "") or ""
        out(f"{str(lot_id):<12} {src:<14} {status_val:<10} {token_amt:>16} {rem:>18} {ep_s:>14} {str(det):<28} {sig[:20]}...")
    out(f"sum_remaining_active_lots_raw = {sum_remaining_active}")
    out()

    # ----- Step 4: Trace wallet deltas (RPC) -----
    out("3️⃣ WALLET DELTA RECONSTRUCTION (from RPC tx history)")
    out("-" * 60)
    rpc = RpcClient(config.rpc_endpoint, timeout_s=getattr(config, "rpc_timeout_s", 20.0), max_retries=config.max_retries)
    sig_list: list = []
    before: str | None = None
    try:
        for _ in range(10):  # up to 10 pages
            batch = rpc.get_signatures_for_address(WALLET, limit=100, before=before)
            if not batch:
                break
            sig_list.extend(batch)
            if len(sig_list) >= 500:
                break
            before = batch[-1].get("signature") if isinstance(batch[-1], dict) else None
            if not before:
                break
    except Exception as e:
        out(f"RPC get_signatures_for_address failed: {e}")
        batch = []

    delta_rows = []
    for sig_info in sig_list[:400]:  # cap to avoid rate limit
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
        ts = _get_block_time(tx)
        ts_s = ts.isoformat() if ts else "N/A"
        sol_d = _parse_sol_delta_lamports(tx, WALLET) or 0
        if delta_raw > 0 and sol_d < 0:
            type_guess = "buy"
        elif delta_raw > 0:
            type_guess = "transfer_in"
        elif delta_raw < 0 and sol_d > 0:
            type_guess = "sell"
        elif delta_raw < 0:
            type_guess = "transfer_out"
        else:
            type_guess = "unknown"
        delta_rows.append((ts, ts_s, sig, delta_raw, type_guess))

    # Sort by time (oldest first)
    delta_rows.sort(key=lambda r: (r[0] or datetime.min.replace(tzinfo=timezone.utc)))

    out(f"{'timestamp':<28} {'tx_signature':<48} {'delta_raw':>18} type_guess")
    for ts, ts_s, sig, d, t in delta_rows:
        out(f"{ts_s:<28} {sig:<48} {d:>18} {t}")
    cumulative = sum(d for _, _, _, d, _ in delta_rows)
    out(f"Cumulative delta from table: {cumulative}")
    out()

    # ----- Step 5: Mismatch transactions -----
    out("4️⃣ MISMATCH ANALYSIS")
    out("-" * 60)
    # Lots in state: we have sum_active_raw. Wallet: wallet_balance_raw.
    # Reconstructed balance from lots = sum of (buy - sells attributed). We don't have "sells attributed" per tx here;
    # we have external_sell ingested. So the gap = wallet_balance_raw - sum_active_raw = diff_raw.
    # If diff_raw > 0: wallet has MORE than lots (missing sells in ledger or transfer-ins without lot).
    # If diff_raw < 0: wallet has LESS than lots (extra sells not debited, or transfer-outs).
    out(f"diff_raw = {diff_raw} (wallet - sum_active_lots)")
    if diff_raw > 0:
        out("Interpretation: wallet balance > lot sum → missing lot(s) for incoming tokens (transfer_in or buy not parsed), or external sells over-attributed.")
    else:
        out("Interpretation: wallet balance < lot sum → sells not debited from lots (external sells not ingested), or transfer-out without sell record.")
    # List txs that are positive delta (incoming) not obviously in lots
    lot_sigs = {getattr(l, "tx_signature", None) for l in lots if getattr(l, "tx_signature", None)}
    incoming = [(ts_s, sig, d, t) for ts, ts_s, sig, d, t in delta_rows if d > 0]
    out(f"Incoming (positive delta) txs: {len(incoming)}")
    unaccounted_incoming = [row for row in incoming if row[1] not in lot_sigs]
    out(f"Incoming txs with no lot in state: {len(unaccounted_incoming)}")
    for ts_s, sig, d, t in unaccounted_incoming[:15]:
        out(f"  tx_signature={sig[:44]} timestamp={ts_s} delta_raw={d} explanation_candidate=transfer_in_or_buy_no_lot")
    out()

    # ----- Step 6: Engine reconstruction in memory -----
    out("5️⃣ ENGINE RECONSTRUCTION (tx-first in memory)")
    out("-" * 60)
    scratch = RuntimeState(
        version=state.version,
        started_at=state.started_at,
        status_file=state.status_file,
        wallet=state.wallet,
        sol=state.sol,
        mints={},
    )
    ms_copy = ms.model_copy(deep=True)
    ms_copy.lots = []
    ms_copy.trading_bag_raw = "0"
    scratch.mints[MINT] = ms_copy
    decimals_by_mint = {MINT: decimals}
    symbol_by_mint = {MINT: SYMBOL}
    n_created = run_tx_first_lot_engine(
        scratch,
        rpc,
        WALLET,
        decimals_by_mint,
        journal_path=None,
        max_signatures=500,
        symbol_by_mint=symbol_by_mint,
        delay_after_request_sec=0.05,
    )
    expected_lots = getattr(scratch.mints[MINT], "lots", None) or []
    expected_sum = sum(int(getattr(l, "remaining_amount", 0) or 0) for l in expected_lots)
    out(f"expected_lots_from_history (scratch run, max_sigs=500): {len(expected_lots)} lots")
    out(f"expected sum remaining (scratch): {expected_sum}")
    out(f"lots_in_state: {len(lots)}")
    out(f"sum_active_lots_raw (state): {sum_active_raw}")
    out(f"Difference (expected_sum - state sum): {expected_sum - sum_active_raw}")
    # Signatures in scratch vs state
    scratch_sigs = {getattr(l, "tx_signature", None) for l in expected_lots if getattr(l, "tx_signature", None)}
    state_sigs = {getattr(l, "tx_signature", None) for l in lots if getattr(l, "tx_signature", None)}
    only_scratch = scratch_sigs - state_sigs
    only_state = state_sigs - scratch_sigs
    out(f"Signatures only in scratch (expected): {len(only_scratch)}")
    out(f"Signatures only in state: {len(only_state)}")
    if only_scratch:
        for s in list(only_scratch)[:5]:
            out(f"  only_scratch sig: {s}")
    if only_state:
        for s in list(only_state)[:5]:
            out(f"  only_state sig: {s}")
    rpc.close()
    out()

    # ----- Step 7: Classify root cause -----
    out("6️⃣ ROOT CAUSE CLASSIFICATION")
    out("-" * 60)
    if diff_raw > 0 and len(unaccounted_incoming) > 0 and expected_sum > sum_active_raw:
        classification = "B) transfer_without_provenance (or buy not parsed; incoming txs without lots)"
    elif diff_raw > 0 and len(only_scratch) > 0:
        classification = "C) engine_scan_window_limit (more buys in full history than state had window for)"
    elif diff_raw > 0:
        classification = "A) external_sell_not_mapped (wallet has more than lots: possible transfer_in without lot)"
    elif diff_raw < 0:
        classification = "A) external_sell_not_mapped (wallet has less than lots: sells not debited from ledger)"
    elif abs(diff_raw) <= 1:
        classification = "sufficient (rounding)"
    else:
        classification = "G) other (see delta table and engine comparison)"
    out(classification)
    out()
    out("7️⃣ AUTO-RESOLVE?")
    if "engine_scan_window_limit" in classification:
        out("Could auto-resolve by re-running tx-backfill with higher max_signatures (if RPC history covers all txs).")
    elif "transfer_without_provenance" in classification:
        out("May require trusted-transfer provenance or manual lot creation; not fully auto-resolvable without code/process.")
    else:
        out("Requires code change or manual reconciliation; see classification.")
    out()
    # Write report file
    report_path = DATA_DIR / "hachi_reconciliation_diagnostic_report.txt"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"Report written to {report_path}")


if __name__ == "__main__":
    main()
