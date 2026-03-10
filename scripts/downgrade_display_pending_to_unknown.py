#!/usr/bin/env python3
"""
One-off: set all display-pending lots to entry_confidence=unknown so they stop showing as pending.
Display-pending = entry_confidence=snapshot and source != initial_migration.
No RPC required. Run from project root: .venv/bin/python3 scripts/downgrade_display_pending_to_unknown.py
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mint_ladder_bot.state import load_state, save_state_atomic


def count_display_pending(state) -> int:
    n = 0
    for ms in getattr(state, "mints", {}).values():
        for lot in getattr(ms, "lots", None) or []:
            ec = getattr(lot, "entry_confidence", None)
            src = getattr(lot, "source", None)
            if ec == "snapshot" and src != "initial_migration":
                n += 1
    return n


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
    before = count_display_pending(state)
    print(f"Display-pending lots: {before}")

    downgraded = 0
    for _mint, ms in getattr(state, "mints", {}).items():
        for lot in getattr(ms, "lots", None) or []:
            ec = getattr(lot, "entry_confidence", None)
            src = getattr(lot, "source", None)
            if ec != "snapshot" or src == "initial_migration":
                continue
            lot.entry_confidence = "unknown"  # type: ignore[assignment]
            lot.entry_price_sol_per_token = None  # type: ignore[assignment]
            lot.cost_basis_confidence = "unknown"  # type: ignore[assignment]
            downgraded += 1

    print(f"Downgraded to unknown: {downgraded}")
    after = count_display_pending(state)
    print(f"Display-pending after: {after}")

    save_state_atomic(state_path, state)
    print("State saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
