#!/usr/bin/env python3
"""
Audit lots in state.json: count by entry_confidence, list pending_price_resolution lots,
flag exact lots with invalid (out-of-range) entry price.
Run from project root: .venv/bin/python3 scripts/audit_pending_lots.py

Categories for pending lots (when resolver does not resolve):
- tx_found_but_invalid: find_buy_tx matched but price failed validate_entry_price (PRICE_SANITY_REJECTED)
- tx_not_found: no matching tx in scan window (TX_LOOKUP_FAILED reason=no_matching_tx_after_scan)
- rpc_lookup_issue: get_signatures_for_address or get_transaction failed (TX_LOOKUP_FAILED reason=get_signatures_for_address|get_transaction_failures)
- delta_mismatch: tx found but token_delta != lot token_amount (TX_DELTA_NOT_MATCHED)
- stale_historical_buy_outside_scan_window: scan limit reached (TX_SCAN_LIMIT_REACHED)
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

from mint_ladder_bot.state import load_state, save_state_atomic
from mint_ladder_bot.runner import validate_entry_price


def fix_invalid_exact_lots(state) -> int:
    """Downgrade exact lots with out-of-range entry_price to unknown. Returns count fixed."""
    fixed = 0
    for _mint, ms in getattr(state, "mints", {}).items():
        lots = getattr(ms, "lots", None) or []
        for lot in lots:
            if getattr(lot, "entry_confidence", None) != "exact":
                continue
            ep = getattr(lot, "entry_price_sol_per_token", None)
            if ep is not None and not validate_entry_price(ep):
                lot.entry_confidence = "unknown"  # type: ignore[assignment]
                lot.entry_price_sol_per_token = None
                lot.cost_basis_confidence = "unknown"  # type: ignore[assignment]
                fixed += 1
    return fixed


def main() -> int:
    state_path = PROJECT_ROOT / "state.json"
    status_path = PROJECT_ROOT / "status.json"
    if not state_path.exists():
        print("state.json not found", file=sys.stderr)
        return 1
    if not status_path.exists():
        print("status.json not found", file=sys.stderr)
        return 1

    state = load_state(state_path, status_path)
    by_confidence: dict[str, list[tuple[str, str, str, str | None]]] = {
        "pending_price_resolution": [],
        "snapshot": [],
        "exact": [],
        "unknown": [],
        "inferred": [],
    }
    exact_invalid: list[tuple[str, str, float]] = []

    for mint, ms in getattr(state, "mints", {}).items():
        lots = getattr(ms, "lots", None) or []
        for lot in lots:
            ec = getattr(lot, "entry_confidence", None) or "unknown"
            lot_id = getattr(lot, "lot_id", "")[:8]
            token_amount = str(getattr(lot, "token_amount", 0) or 0)
            entry = getattr(lot, "entry_price_sol_per_token", None)
            key = ec if ec in by_confidence else "unknown"
            by_confidence[key].append((mint[:12], lot_id, token_amount, str(entry) if entry is not None else None))
            if ec == "exact" and entry is not None:
                if not validate_entry_price(entry):
                    exact_invalid.append((mint[:12], lot_id, entry))

    # Report
    print("=== Lot audit by entry_confidence ===\n")
    for label in ["pending_price_resolution", "snapshot", "exact", "unknown", "inferred"]:
        items = by_confidence.get(label, [])
        print(f"{label}: {len(items)}")
        if label == "pending_price_resolution" and items:
            for mint, lot_id, token_amount, entry in items:
                print(f"  mint={mint} lot_id={lot_id} token_amount={token_amount} entry={entry}")
    print()

    if exact_invalid:
        print("=== Exact lots with invalid entry price (out of range) ===\n")
        for mint, lot_id, entry in exact_invalid:
            print(f"  mint={mint} lot_id={lot_id} entry_price={entry:.6e}")
        print()

    pending_n = len(by_confidence.get("pending_price_resolution", []))
    print("Categories for pending_price_resolution (see run.log / events when resolver runs):")
    print("  - tx_found_but_invalid: PRICE_SANITY_REJECTED")
    print("  - tx_not_found: no_matching_tx_after_scan")
    print("  - rpc_lookup_issue: get_signatures_for_address / get_transaction_failures")
    print("  - delta_mismatch: TX_DELTA_NOT_MATCHED")
    print("  - stale_historical_buy_outside_scan_window: TX_SCAN_LIMIT_REACHED")
    if pending_n > 0:
        print("\nRun: .venv/bin/python3 scripts/run_resolver_once.py")
        print("Then check run.log for TX_LOOKUP_FAILED / PRICE_SANITY_REJECTED / PENDING_LOT_RESOLVED.")

    if exact_invalid and "--fix" in sys.argv:
        n = fix_invalid_exact_lots(state)
        if n:
            save_state_atomic(state_path, state)
            print(f"Fixed {n} exact lot(s) with invalid entry price (downgraded to unknown).")
    elif exact_invalid:
        print("To downgrade invalid exact lots to unknown, run: scripts/audit_pending_lots.py --fix")
    return 0


if __name__ == "__main__":
    sys.exit(main())
