#!/usr/bin/env python3
"""
Investigate Feb 23 → Mar 7 $HACHI provenance gap.

For every merged-history tx in the window, classify:
- delta_raw for $HACHI, detected via (wallet/token/both), parser classification,
  did lot engine create a lot?, exact reason if no.

Output: chronological table + totals + root cause + recommendation.
No live state modification.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _load_env() -> None:
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

WALLET = "3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR"
HACHI_MINT = "x95HN3DWvbfCBtTjGm587z8suK3ec6cwQwgZNLbWKyp"
# Gap window: start 2026-02-23T18:23:23Z, end 2026-03-07T20:46:16Z (exclusive of end = first lot)
START_TS = 1771871003  # 2026-02-23T18:23:23Z
END_TS = 1772732776    # 2026-03-07T20:46:16Z

DATA_DIR = _REPO / "runtime" / "projects" / "mint_ladder_bot"
REBUILD_STATE_PATH = DATA_DIR / "state_full_history_rebuild.json"
STATUS_PATH = DATA_DIR / "status.json"
SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PAGE_SIZE = 1000


def _fetch_all_signatures(rpc: Any, address: str) -> List[Dict[str, Any]]:
    out = []
    before = None
    while True:
        batch = rpc.get_signatures_for_address(address, limit=PAGE_SIZE, before=before)
        if not batch:
            break
        for item in batch:
            out.append({"signature": item.get("signature"), "slot": item.get("slot"), "blockTime": item.get("blockTime")})
        if len(batch) < PAGE_SIZE:
            break
        before = batch[-1].get("signature")
        if not before:
            break
    return out


def _get_token_account_for_mint(rpc: Any, wallet: str, mint: str) -> Optional[str]:
    client = rpc._client_for(rpc._primary)
    token_accounts = []
    for program_id in (SPL_TOKEN, TOKEN_2022):
        try:
            token_accounts.extend(client.get_token_accounts_by_owner(wallet, program_id=program_id))
        except Exception:
            continue
    for item in token_accounts:
        try:
            data = (item.get("account") or {}).get("data") or {}
            info = (data.get("parsed") or {}).get("info") or {}
            if info.get("mint") == mint:
                return item.get("pubkey")
        except Exception:
            continue
    return None


def _build_merged_sorted_in_window(
    rpc: Any,
    wallet: str,
    target_mint: str,
    start_ts: int,
    end_ts: int,
) -> Tuple[List[Dict[str, Any]], Set[str], Set[str]]:
    """Returns (sorted_sig_list in window, wallet_sigs_set, token_account_sigs_set)."""
    wallet_sigs = _fetch_all_signatures(rpc, wallet)
    token_account = _get_token_account_for_mint(rpc, wallet, target_mint)
    ta_sigs = _fetch_all_signatures(rpc, token_account) if token_account else []
    wallet_sig_set = {s["signature"] for s in wallet_sigs if s.get("signature")}
    ta_sig_set = {s["signature"] for s in ta_sigs if s.get("signature")}
    all_by_sig = {}
    for s in wallet_sigs:
        sig = s.get("signature")
        if sig:
            all_by_sig[sig] = s
    for s in ta_sigs:
        sig = s.get("signature")
        if sig:
            all_by_sig[sig] = s
    # Filter to blockTime in [start_ts, end_ts)
    in_window = []
    for s in all_by_sig.values():
        bt = s.get("blockTime")
        if bt is not None and start_ts <= bt < end_ts:
            in_window.append(s)
    in_window.sort(key=lambda x: (x.get("slot") or 0, x.get("blockTime") or 0))
    return in_window, wallet_sig_set, ta_sig_set


def _classify_tx(
    tx: dict,
    wallet: str,
    signature: str,
    mint: str,
    delta_raw: int,
    decimals: int,
) -> str:
    """One of: buy, sell, transfer_in, transfer_out, likely_swap_unparsed, unknown."""
    from mint_ladder_bot.tx_lot_engine import _parse_buy_events_from_tx
    from mint_ladder_bot.tx_infer import parse_sell_events_from_tx, _parse_sol_delta_lamports

    mints_tracked = {mint}
    decimals_by_mint = {mint: decimals}
    buy_events = _parse_buy_events_from_tx(tx, wallet, signature, mints_tracked, decimals_by_mint)
    sell_events = parse_sell_events_from_tx(tx, wallet, mints_tracked, signature)
    has_buy = any(getattr(e, "mint", None) == mint for e in buy_events)
    has_sell = any(getattr(e, "mint", None) == mint for e in sell_events)

    if has_buy:
        return "buy"
    if has_sell:
        return "sell"
    sol_delta = _parse_sol_delta_lamports(tx, wallet)
    if delta_raw > 0:
        if sol_delta is not None and sol_delta < 0:
            return "likely_swap_unparsed"  # SOL out + token in but parser didn't emit buy
        return "transfer_in"
    if delta_raw < 0:
        if sol_delta is not None and sol_delta > 0:
            return "likely_swap_unparsed"  # SOL in + token out but parser didn't emit sell
        return "transfer_out"
    return "unknown"


def _reason_no_lot(classification: str, delta_raw: int, buy_events_any: bool) -> str:
    if delta_raw <= 0:
        return "negative_or_zero_delta"
    if classification == "buy":
        return "buy_but_rejected_by_engine"  # e.g. price validation
    if classification == "transfer_in":
        return "transfer_in_not_promoted"
    if classification == "likely_swap_unparsed":
        return "parser_gap_swap_unparsed"
    if classification == "sell":
        return "n/a_positive_delta_sell_mismatch"
    return "unknown_classification"


def main() -> int:
    from mint_ladder_bot.config import Config
    from mint_ladder_bot.backfill_rpc import BackfillRpcClient
    from mint_ladder_bot.models import StatusFile
    from mint_ladder_bot.state import load_state
    from mint_ladder_bot.tx_infer import _parse_token_deltas_for_mints
    from mint_ladder_bot.tx_lot_engine import _parse_buy_events_from_tx

    config = Config()
    delay_sec = max(0.0, min(int(os.environ.get("TX_BACKFILL_DELAY_MS", "200")) / 1000.0, 2.0))
    primary = (os.environ.get("RPC_PRIMARY") or "").strip() or config.rpc_endpoint
    pool_list = [u.strip() for u in (os.environ.get("RPC_BACKFILL_POOL") or "").strip().split(",") if u.strip()]
    rpc = BackfillRpcClient(
        primary_endpoint=primary,
        pool_endpoints=pool_list,
        timeout_s=getattr(config, "rpc_timeout_s", 20.0),
        delay_after_request_sec=delay_sec,
        max_retries_per_endpoint=2,
    )

    status_path = STATUS_PATH
    rebuild_path = REBUILD_STATE_PATH
    status = StatusFile.model_validate_json(status_path.read_text())
    decimals_by_mint = {m.mint: getattr(m, "decimals", 6) for m in status.mints}
    decimals = decimals_by_mint.get(HACHI_MINT, 6)

    # Rebuild state: which signatures became lots for HACHI
    lot_sigs: Set[str] = set()
    if rebuild_path.exists():
        state_rebuild = load_state(rebuild_path, status_path)
        ms = state_rebuild.mints.get(HACHI_MINT)
        if ms:
            for lot in getattr(ms, "lots", None) or []:
                sig = getattr(lot, "tx_signature", None)
                if sig:
                    lot_sigs.add(sig)

    print("Building merged sig list in gap window [2026-02-23T18:23:23Z, 2026-03-07T20:46:16Z)...")
    sig_list, wallet_sig_set, ta_sig_set = _build_merged_sorted_in_window(
        rpc, WALLET, HACHI_MINT, START_TS, END_TS
    )
    print(f"  Signatures in window: {len(sig_list)}")

    rows: List[Dict[str, Any]] = []
    total_positive_delta = 0
    positive_delta_became_lots = 0
    positive_delta_unexplained = 0

    mints_tracked = {HACHI_MINT}
    for i, sig_info in enumerate(sig_list):
        signature = sig_info.get("signature")
        if not signature:
            continue
        try:
            tx = rpc.get_transaction(signature)
        except Exception as e:
            rows.append({
                "timestamp": "N/A",
                "signature": signature[:16] + "...",
                "slot": sig_info.get("slot"),
                "delta_raw": None,
                "detected_via": "wallet" if signature in wallet_sig_set else ("token_account" if signature in ta_sig_set else "?"),
                "classification": "unknown",
                "lot_created": False,
                "reason_no_lot": f"get_transaction_failed:{e!r}"[:60],
            })
            continue
        if not tx:
            rows.append({
                "timestamp": "N/A",
                "signature": signature[:16] + "...",
                "slot": sig_info.get("slot"),
                "delta_raw": None,
                "detected_via": "wallet" if signature in wallet_sig_set else ("token_account" if signature in ta_sig_set else "?"),
                "classification": "unknown",
                "lot_created": False,
                "reason_no_lot": "tx_null",
            })
            continue

        delta_raw = _parse_token_deltas_for_mints(tx, WALLET, [HACHI_MINT]).get(HACHI_MINT, 0)
        block_time = tx.get("blockTime")
        ts_str = datetime.fromtimestamp(block_time, tz=timezone.utc).isoformat() if block_time else "N/A"

        if delta_raw == 0:
            # Only include if mint appears in tx (e.g. transfer between other accounts)
            meta = tx.get("meta") or {}
            any_mint = any(
                b.get("mint") == HACHI_MINT
                for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or [])
            )
            if not any_mint:
                continue  # skip txs with no HACHI involvement
            classification = "unknown"
        else:
            classification = _classify_tx(tx, WALLET, signature, HACHI_MINT, delta_raw, decimals)

        if delta_raw > 0:
            total_positive_delta += delta_raw
            if signature in lot_sigs:
                positive_delta_became_lots += delta_raw
            else:
                positive_delta_unexplained += delta_raw

        detected = "both" if (signature in wallet_sig_set and signature in ta_sig_set) else ("wallet" if signature in wallet_sig_set else "token_account")
        lot_created = signature in lot_sigs
        buy_events = _parse_buy_events_from_tx(tx, WALLET, signature, mints_tracked, {HACHI_MINT: decimals})
        has_buy = any(getattr(e, "mint", None) == HACHI_MINT for e in buy_events)
        reason = ""
        if not lot_created and delta_raw > 0:
            reason = _reason_no_lot(classification, delta_raw, has_buy)
        elif not lot_created and delta_raw <= 0:
            reason = "negative_or_zero_delta"

        rows.append({
            "timestamp": ts_str,
            "signature": signature[:22],
            "slot": sig_info.get("slot"),
            "delta_raw": delta_raw,
            "detected_via": detected,
            "classification": classification,
            "lot_created": lot_created,
            "reason_no_lot": reason or "",
        })

    rpc.close()

    # Print table
    print("\n" + "=" * 120)
    print("CHRONOLOGICAL TABLE — $HACHI transactions in gap 2026-02-23T18:23:23Z → 2026-03-07T20:46:16Z")
    print("=" * 120)
    fmt = "{:<28} {:<24} {:<10} {:<18} {:<12} {:<22} {:<8} {:<30}"
    print(fmt.format("timestamp", "signature", "slot", "delta_raw", "detected_via", "classification", "lot?", "reason_no_lot"))
    print("-" * 120)
    for r in rows:
        print(fmt.format(
            str(r["timestamp"])[:28],
            str(r["signature"])[:24],
            str(r["slot"])[:10],
            str(r["delta_raw"])[:18] if r["delta_raw"] is not None else "N/A",
            str(r["detected_via"])[:12],
            str(r["classification"])[:22],
            "yes" if r["lot_created"] else "no",
            str(r["reason_no_lot"])[:30],
        ))
    print("=" * 120)

    # Totals
    print("\nTOTALS (positive delta in gap):")
    print(f"  total positive delta_raw in gap:     {total_positive_delta}")
    print(f"  total positive delta_raw -> lots:   {positive_delta_became_lots}")
    print(f"  total positive delta_raw unexplained: {positive_delta_unexplained}")

    # Classify by reason
    by_class = {}
    by_reason = {}
    for r in rows:
        if r["delta_raw"] and r["delta_raw"] > 0 and not r["lot_created"]:
            by_class[r["classification"]] = by_class.get(r["classification"], 0) + 1
            by_reason[r["reason_no_lot"]] = by_reason.get(r["reason_no_lot"], 0) + 1

    print("\nUNEXPLAINED POSITIVE DELTA — count by classification:")
    for k, v in sorted(by_class.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    print("UNEXPLAINED — count by reason_no_lot:")
    for k, v in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")

    # Dominant root cause
    if by_reason:
        dominant_reason = max(by_reason.items(), key=lambda x: x[1])
        if "transfer_in" in dominant_reason[0]:
            root = "A) transfer-ins not promoted to sellable provenance"
        elif "parser_gap" in dominant_reason[0] or "likely_swap" in str(by_class):
            root = "B) parser gap for buy/swap tx shape"
        elif "source" in dominant_reason[0] or "external" in dominant_reason[0]:
            root = "C) external source coverage still incomplete"
        else:
            root = "D) mixed causes"
    else:
        root = "No unexplained positive delta in window (all positive delta became lots or window empty)."

    print("\nDOMINANT ROOT CAUSE:")
    print(f"  {root}")

    # Recommendation
    if "transfer_in" in root or (by_reason.get("transfer_in_not_promoted", 0) or 0) > 0:
        rec = "next step should be transfer-provenance promotion"
    elif "parser gap" in root or (by_reason.get("parser_gap_swap_unparsed", 0) or 0) > 0 or (by_class.get("likely_swap_unparsed", 0) or 0) > 0:
        rec = "next step should be parser fix"
    elif "source" in root or "incomplete" in root:
        rec = "next step should be deeper source integration"
    else:
        rec = "mixed follow-up required"

    print("\nRECOMMENDATION:")
    print(f"  {rec}")

    # Write report file
    out_path = DATA_DIR / "hachi_gap_investigation_report.txt"
    buf = []
    buf.append("HACHI GAP INVESTIGATION — Feb 23 → Mar 7")
    buf.append("")
    buf.append(fmt.format("timestamp", "signature", "slot", "delta_raw", "detected_via", "classification", "lot?", "reason_no_lot"))
    buf.append("-" * 120)
    for r in rows:
        buf.append(fmt.format(
            str(r["timestamp"])[:28], str(r["signature"])[:24], str(r["slot"])[:10],
            str(r["delta_raw"])[:18] if r["delta_raw"] is not None else "N/A",
            str(r["detected_via"])[:12], str(r["classification"])[:22],
            "yes" if r["lot_created"] else "no", str(r["reason_no_lot"])[:30],
        ))
    buf.append("")
    buf.append(f"total_positive_delta_raw={total_positive_delta} positive_became_lots={positive_delta_became_lots} unexplained={positive_delta_unexplained}")
    buf.append(f"dominant_root={root}")
    buf.append(f"recommendation={rec}")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(buf), encoding="utf-8")
        print(f"\nReport written to {out_path}")
    except Exception as e:
        print(f"\nCould not write report: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
