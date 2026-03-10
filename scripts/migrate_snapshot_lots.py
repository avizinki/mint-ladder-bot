#!/usr/bin/env python3
"""
Migrate legacy wallet_buy_detected lots from entry_confidence=snapshot to unknown.

Rule: wallet_buy_detected must never use snapshot. initial_migration lots are not modified.

Usage:
  python scripts/migrate_snapshot_lots.py [--state PATH] [--dry-run]

Creates a timestamped backup before modifying state unless --dry-run.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate wallet_buy_detected snapshot lots to unknown")
    parser.add_argument("--state", type=Path, default=None, help="Path to state.json (default: ./state.json)")
    parser.add_argument("--dry-run", action="store_true", help="Only report what would be changed")
    args = parser.parse_args()

    state_path = args.state
    if state_path is None:
        state_path = Path("state.json")
    state_path = state_path.resolve()
    if not state_path.exists():
        print(f"Error: state file not found: {state_path}", file=sys.stderr)
        return 1

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    mints = state.get("mints")
    if not isinstance(mints, dict):
        print("Error: state has no mints dict", file=sys.stderr)
        return 1

    migrated = 0
    for mint, ms in mints.items():
        if not isinstance(ms, dict):
            continue
        lots = ms.get("lots")
        if not isinstance(lots, list):
            continue
        for lot in lots:
            if not isinstance(lot, dict):
                continue
            source = lot.get("source")
            if source == "initial_migration":
                continue
            if source != "wallet_buy_detected":
                continue
            ec = lot.get("entry_confidence")
            if ec != "snapshot":
                continue
            lot["entry_confidence"] = "unknown"
            migrated += 1

    print(f"Migrated {migrated} lots (wallet_buy_detected + snapshot -> unknown)")
    if migrated == 0:
        return 0
    if args.dry_run:
        print("Dry run: no file written")
        return 0

    backup_path = state_path.parent / f"state.json.bak.migrate_snapshot_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            backup_content = f.read()
        backup_path.write_text(backup_content, encoding="utf-8")
        print(f"Backup: {backup_path}")
    except Exception as e:
        print(f"Error: could not create backup: {e}", file=sys.stderr)
        return 1

    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        print(f"Wrote {state_path}")
    except Exception as e:
        print(f"Error: could not write state: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
