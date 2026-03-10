#!/usr/bin/env python3
"""
Ledger integrity: mark duplicate lots as duplicate_explained so they do not affect trading bag or dashboard trust.

1) wallet_buy_detected/snapshot lots whose token_amount is already explained by tx_exact lots (subset sum).
2) wallet_buy_detected lots that have the same token_amount as an existing tx_exact lot (exact match).
3) Multiple wallet_buy_detected lots with the same token_amount: keep first, mark rest duplicate_explained.

Target mints: all; known bad cases include WAR, HACHI, PUSH, 丙午 (DMYNp65mub3i).

Run: .venv/bin/python3 scripts/cleanup_duplicate_fallback_lots.py [--dry-run] [--journal PATH]
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _delta_explained_by_tx_exact_amounts(tx_exact_amounts: list[int], delta_raw: int) -> bool:
    if delta_raw <= 0 or not tx_exact_amounts:
        return False
    tolerance = max(1, int(delta_raw * 0.01))
    for r in range(1, len(tx_exact_amounts) + 1):
        for subset in itertools.combinations(tx_exact_amounts, r):
            if abs(sum(subset) - delta_raw) <= tolerance:
                return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Do not write state.json")
    ap.add_argument("--journal", type=Path, default=None, help="Event journal path to append DUPLICATE_LOT_CLEANED")
    args = ap.parse_args()
    state_path = PROJECT_ROOT / "state.json"
    if not state_path.exists():
        print("state.json not found", file=sys.stderr)
        return 1
    state = json.loads(state_path.read_text())
    mints = state.get("mints") or {}
    changed = False
    cleaned: list[tuple[str, str, str]] = []  # (mint_short, lot_id, token_amount)

    for mint, ms in mints.items():
        if not isinstance(ms, dict):
            continue
        lots = ms.get("lots") or []
        tx_exact_amounts = []
        for lot in lots:
            if lot.get("source") not in ("tx_exact", "tx_parsed"):
                continue
            if lot.get("status") != "active":
                continue
            try:
                a = int(lot.get("token_amount") or 0)
                tx_exact_amounts.append(a)
            except (ValueError, TypeError):
                pass
        tx_exact_amounts_set = set(tx_exact_amounts)

        # (1)(2): wallet_buy_detected/snapshot explained by tx_exact (exact match or subset sum)
        for lot in lots:
            if lot.get("status") != "active":
                continue
            if lot.get("source") not in ("wallet_buy_detected", "snapshot"):
                continue
            try:
                amt = int(lot.get("token_amount") or 0)
            except (ValueError, TypeError):
                continue
            if amt <= 0:
                continue
            if amt in tx_exact_amounts_set or _delta_explained_by_tx_exact_amounts(tx_exact_amounts, amt):
                lot_id_short = (lot.get("lot_id") or "")[:8]
                if args.dry_run:
                    print(f"DRY-RUN would mark mint={mint[:12]} lot_id={lot_id_short} token_amount={amt} as duplicate_explained")
                else:
                    lot["status"] = "duplicate_explained"
                    changed = True
                    cleaned.append((mint[:12], lot_id_short, str(amt)))
                    print(f"DUPLICATE_LOT_CLEANED mint={mint[:12]} lot_id={lot_id_short} token_amount={amt}")

        # (3): multiple wallet_buy_detected with same amount — keep first, mark rest
        seen_wbd: dict[int, bool] = {}
        for lot in lots:
            if lot.get("status") != "active":
                continue
            if lot.get("source") != "wallet_buy_detected":
                continue
            try:
                amt = int(lot.get("token_amount") or 0)
            except (ValueError, TypeError):
                continue
            if amt <= 0:
                continue
            if amt in seen_wbd:
                lot_id_short = (lot.get("lot_id") or "")[:8]
                if args.dry_run:
                    print(f"DRY-RUN would mark duplicate same-amount mint={mint[:12]} lot_id={lot_id_short} token_amount={amt}")
                else:
                    lot["status"] = "duplicate_explained"
                    changed = True
                    cleaned.append((mint[:12], lot_id_short, str(amt)))
                    print(f"DUPLICATE_LOT_CLEANED mint={mint[:12]} lot_id={lot_id_short} token_amount={amt} (same-amount duplicate)")
            else:
                seen_wbd[amt] = True

    if changed and not args.dry_run:
        state_path.write_text(json.dumps(state, indent=2))
        print(f"Wrote state.json; marked {len(cleaned)} lot(s) as duplicate_explained")
        if args.journal and args.journal.parent.exists():
            try:
                from mint_ladder_bot.events import append_event, EVENT_DUPLICATE_LOT_CLEANED
                for mint_short, lot_id_short, amt in cleaned:
                    append_event(args.journal, EVENT_DUPLICATE_LOT_CLEANED, {"mint": mint_short, "lot_id": lot_id_short, "token_amount": amt})
            except Exception as e:
                print(f"Journal append failed: {e}", file=sys.stderr)
    elif args.dry_run and cleaned:
        print(f"Dry-run: would mark {len(cleaned)} lot(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
