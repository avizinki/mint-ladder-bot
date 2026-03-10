#!/usr/bin/env python3
"""
Reconcile balance-delta / mystery-money cases for a mint in a time window.

Prints: observed balance deltas, matching txs searched, matched tx if found,
event sequence, whether any lot/state mutation happened, and final classification:
  matched_tx | unresolved_informational | duplicate_ignored | parser_bug

Usage:
  python tools/reconcile_balance_delta.py --mint 2eMYCijQY4ZM --from "2026-03-08T13:52:00Z" --to "2026-03-08T14:00:00Z"
  python tools/reconcile_balance_delta.py --mint EKwF2HD6X4rH --from "2026-03-08T13:52:00" --to "2026-03-08T14:05:00" --state state.json --status status.json --events events.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _parse_ts(s: str) -> float:
    """Parse ISO-ish ts to unix timestamp for comparison."""
    from datetime import datetime, timezone
    s = (s or "").strip().replace("Z", "+00:00")
    # Support 2026-03-08T13:52:56.952603+00:00 and 2026-03-08T13:52:56+00:00
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.timestamp()
        except ValueError:
            continue
    try:
        dt = datetime.strptime(s.replace("+00:00", "").rstrip("Z"), "%Y-%m-%dT%H:%M:%S.%f")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        pass
    try:
        dt = datetime.strptime(s.replace("+00:00", "").rstrip("Z"), "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        pass
    return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile balance delta for a mint in a time window.")
    ap.add_argument("--mint", required=True, help="Mint address (full or short 2eMYCi...Y4ZM)")
    ap.add_argument("--from", dest="ts_from", required=True, help="Start time (ISO or YYYY-MM-DDTHH:MM:SS)")
    ap.add_argument("--to", dest="ts_to", required=True, help="End time (ISO)")
    ap.add_argument("--state", type=Path, default=None, help="state.json path")
    ap.add_argument("--status", type=Path, default=None, help="status.json path")
    ap.add_argument("--events", type=Path, default=None, help="events.jsonl path")
    ap.add_argument("--project-root", type=Path, default=ROOT, help="Project root for default paths")
    args = ap.parse_args()
    root = args.project_root
    state_path = args.state or root / "state.json"
    status_path = args.status or root / "status.json"
    events_path = args.events or root / "events.jsonl"

    mint_arg = args.mint.strip()
    ts_from = _parse_ts(args.ts_from)
    ts_to = _parse_ts(args.ts_to)
    if ts_from <= 0 or ts_to <= 0:
        print("Invalid --from / --to timestamps.", file=sys.stderr)
        return 1

    # Resolve mint to full address if short
    mint_full = mint_arg
    if status_path.exists():
        data = json.loads(status_path.read_text())
        for m in (data.get("mints") or []):
            if isinstance(m, dict) and m.get("mint"):
                maddr = m["mint"]
                if maddr == mint_arg or maddr.startswith(mint_arg) or mint_arg in maddr:
                    mint_full = maddr
                    break

    events_in_window: list[dict] = []
    if events_path.exists():
        for line in events_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts_s = (rec.get("ts") or "").replace("Z", "+00:00")
                t = _parse_ts(ts_s) if ts_s else 0
                if ts_from <= t <= ts_to:
                    ev_mint = (rec.get("mint") or "").strip()
                    if not ev_mint:
                        continue
                    if ev_mint in (mint_full, mint_arg) or mint_full.startswith(ev_mint) or ev_mint in mint_full:
                        events_in_window.append(rec)
            except json.JSONDecodeError:
                continue

    # Classify event sequence
    has_tx_lookup_failed = any(r.get("event") == "TX_LOOKUP_FAILED" for r in events_in_window)
    has_buy_detected_no_tx = any(r.get("event") == "BUY_DETECTED_NO_TX" for r in events_in_window)
    has_unresolved = any(r.get("event") == "UNRESOLVED_BALANCE_DELTA" for r in events_in_window)
    has_balance_delta_without_tx = any(r.get("event") == "BALANCE_DELTA_WITHOUT_TX" for r in events_in_window)
    has_informational_only = any(r.get("event") == "BALANCE_DELTA_INFORMATIONAL_ONLY" for r in events_in_window)
    has_lot_created = any(r.get("event") in ("LOT_CREATED", "LOT_CREATED_TX_EXACT", "LOT_CREATED_FROM_TX") for r in events_in_window)
    has_state_mismatch = any(r.get("event") == "STATE_BALANCE_MISMATCH" for r in events_in_window)
    has_mint_holding = any(r.get("event") == "MINT_HOLDING_EXPLANATION" for r in events_in_window)

    # Observed deltas from events
    unmatched_rawn = [r.get("unmatched_raw") or r.get("delta_raw") or r.get("amount_raw") for r in events_in_window]
    unmatched_rawn = [int(x) for x in unmatched_rawn if x is not None]

    print("=== Reconcile balance delta ===")
    print(f"mint: {mint_full}")
    print(f"window: {args.ts_from} -> {args.ts_to}")
    print()
    print("--- Events in window ---")
    for r in events_in_window:
        print(json.dumps(r, default=str))
    print()
    print("--- Observed deltas (unmatched_raw / delta_raw) ---")
    if unmatched_rawn:
        print(" ", list(set(unmatched_rawn)))
    else:
        print("  (none in events)")
    print()
    print("--- State (if available) ---")
    lot_mutation = False
    if state_path.exists():
        state = json.loads(state_path.read_text())
        mints = state.get("mints") or {}
        ms = mints.get(mint_full) or mints.get(mint_arg)
        if ms:
            lots = ms.get("lots") or []
            print(f"  lots: {len(lots)}")
            for i, lot in enumerate(lots):
                src = lot.get("source", "?")
                sig = (lot.get("tx_signature") or "")[:16]
                print(f"    lot {i+1}: source={src} tx_sig={sig}... remaining={lot.get('remaining_amount')}")
            if lots:
                lot_mutation = True
        else:
            print("  (mint not in state)")
    else:
        print("  (no state.json)")
    print()
    print("--- Classification ---")
    if has_lot_created and not has_tx_lookup_failed:
        classification = "matched_tx"
        print("  matched_tx (lot created from tx; no TX_LOOKUP_FAILED)")
    elif has_tx_lookup_failed or has_buy_detected_no_tx or has_unresolved or has_balance_delta_without_tx:
        if has_lot_created:
            classification = "parser_bug"
            print("  parser_bug (both lot created and TX_LOOKUP_FAILED/unresolved — inconsistent)")
        else:
            classification = "unresolved_informational"
            print("  unresolved_informational (no matching tx; no tradable lot; events informational only)")
    elif has_state_mismatch or has_mint_holding:
        classification = "unresolved_informational"
        print("  unresolved_informational (STATE_BALANCE_MISMATCH / MINT_HOLDING_EXPLANATION; wallet balance != sum lots)")
    else:
        classification = "unknown"
        print("  unknown (no relevant events in window)")
    print()
    print("--- Summary ---")
    print(f"  Event count: {len(events_in_window)}")
    print(f"  Lot created in window: {has_lot_created}")
    print(f"  State mutation (lots present): {lot_mutation}")
    print(f"  Final classification: {classification}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
