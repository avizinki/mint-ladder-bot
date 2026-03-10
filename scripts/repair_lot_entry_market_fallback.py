#!/usr/bin/env python3
"""
One-off repair: clear lot.entry_price_sol_per_token when it was incorrectly set from
market/bootstrap (token→token fallback). Creates timestamped backup before mutating.

Criteria for clearing:
- lot.source in (tx_parsed, tx_exact)
- mint has market-derived entry (bootstrap_from_market or entry_source == "market_bootstrap")
- lot.entry_price matches mint-level entry (within relative tolerance)

After repair: lot.entry_price_sol_per_token = None, entry_confidence = "unknown".
Emits LOT_ENTRY_REPAIR_CLEARED_MARKET_FALLBACK to events.jsonl.

Usage (from project root):
  .venv/bin/python3 scripts/repair_lot_entry_market_fallback.py [--dry-run] [--state PATH] [--events PATH]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mint_ladder_bot.state import load_state, save_state_atomic
from mint_ladder_bot.events import append_event, LOT_ENTRY_REPAIR_CLEARED_MARKET_FALLBACK

# Relative tolerance for "entry equals mint bootstrap"
ENTRY_MATCH_REL_TOL = 1e-6


def _mint_has_market_bootstrap(ms) -> bool:
    if getattr(ms, "bootstrap_from_market", False):
        return True
    if getattr(ms, "entry_source", None) == "market_bootstrap":
        return True
    return False


def _entry_matches(lot_entry: float | None, mint_entry: float | None) -> bool:
    if lot_entry is None or mint_entry is None or mint_entry <= 0:
        return False
    if lot_entry <= 0:
        return False
    rel = abs(lot_entry - mint_entry) / mint_entry
    return rel <= ENTRY_MATCH_REL_TOL


def main() -> int:
    ap = argparse.ArgumentParser(description="Repair lots with market-fallback entry (backup first).")
    ap.add_argument("--dry-run", action="store_true", help="Do not write state or events")
    ap.add_argument("--state", type=Path, default=PROJECT_ROOT / "state.json", help="state.json path")
    ap.add_argument("--events", type=Path, default=None, help="events.jsonl path (default: same dir as state)")
    args = ap.parse_args()
    state_path = args.state
    events_path = args.events or state_path.parent / "events.jsonl"

    if not state_path.exists():
        print("state.json not found", file=sys.stderr)
        return 1
    status_path = state_path.parent / "status.json"
    if not status_path.exists():
        status_path = PROJECT_ROOT / "status.json"
    if not status_path.exists():
        print("status.json not found", file=sys.stderr)
        return 1

    state = load_state(state_path, status_path)

    # Timestamped backup (unless dry-run)
    if not args.dry_run:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = state_path.with_suffix(state_path.suffix + f".repair_backup_{ts}")
        shutil.copy2(state_path, backup_path)
        print(f"Backup: {backup_path}")
        if events_path.exists():
            backup_events = events_path.with_suffix(events_path.suffix + f".repair_backup_{ts}")
            shutil.copy2(events_path, backup_events)
            print(f"Backup: {backup_events}")

    cleared = 0
    for mint_addr, ms in getattr(state, "mints", {}).items():
        if not _mint_has_market_bootstrap(ms):
            continue
        mint_entry = getattr(ms, "entry_price_sol_per_token", None) or getattr(ms, "working_entry_price_sol_per_token", None)
        for lot in getattr(ms, "lots", None) or []:
            src = getattr(lot, "source", None)
            if src not in ("tx_parsed", "tx_exact"):
                continue
            lot_entry = getattr(lot, "entry_price_sol_per_token", None)
            if lot_entry is None:
                continue
            if not _entry_matches(lot_entry, mint_entry):
                continue
            # Clear: entry was market fallback
            lot.entry_price_sol_per_token = None  # type: ignore[assignment]
            lot.entry_confidence = "unknown"  # type: ignore[assignment]
            if getattr(lot, "cost_basis_confidence", None) is not None:
                lot.cost_basis_confidence = "unknown"  # type: ignore[assignment]
            cleared += 1
            payload = {
                "mint": mint_addr[:12],
                "tx_sig": (getattr(lot, "tx_signature", None) or "")[:16],
                "lot_id": (getattr(lot, "lot_id", None) or "")[:8],
                "method": "repair_cleared_market_fallback",
                "entry": None,
                "reason": "lot_entry_matched_mint_bootstrap_market",
            }
            print(f"CLEARED mint={mint_addr[:12]} lot_id={payload['lot_id']} tx_sig={payload['tx_sig']}")
            if not args.dry_run and events_path:
                append_event(events_path, LOT_ENTRY_REPAIR_CLEARED_MARKET_FALLBACK, payload)

    print(f"Cleared {cleared} lot(s)")
    if cleared and not args.dry_run:
        save_state_atomic(state_path, state)
        print("State saved.")
    elif args.dry_run and cleared:
        print("Dry-run: no state or events written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
