#!/usr/bin/env python3
"""
Migrate state to CEO sell-accounting split: sold_bot_raw vs sold_external_raw.

Classifies executed_steps:
- key.startswith("ext_") → external
- else → bot

Populates sold_bot_raw and sold_external_raw per mint. Creates timestamped backup
unless --dry-run.

Usage (from project root):
  .venv/bin/python3 scripts/migrate_sell_accounting.py [--dry-run] [--state PATH]
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


def _classify_sells(executed_steps: dict) -> tuple[int, int]:
    """Return (sold_bot_raw, sold_external_raw) from executed_steps."""
    bot_raw = 0
    ext_raw = 0
    for step_key, step_info in (executed_steps or {}).items():
        try:
            amt = int(getattr(step_info, "sold_raw", 0) or 0)
        except (ValueError, TypeError):
            amt = 0
        if isinstance(step_key, str) and step_key.startswith("ext_"):
            ext_raw += amt
        else:
            bot_raw += amt
    return bot_raw, ext_raw


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate state: set sold_bot_raw / sold_external_raw from executed_steps.")
    ap.add_argument("--dry-run", action="store_true", help="Do not write state or backup")
    ap.add_argument("--state", type=Path, default=PROJECT_ROOT / "state.json", help="state.json path")
    args = ap.parse_args()
    state_path = args.state

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

    if not args.dry_run:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = state_path.with_suffix(state_path.suffix + f".migrate_sell_accounting_{ts}")
        shutil.copy2(state_path, backup_path)
        print(f"Backed up state to {backup_path}")

    updated = 0
    for mint_addr, ms in state.mints.items():
        steps = getattr(ms, "executed_steps", None) or {}
        bot_raw, ext_raw = _classify_sells(steps)
        ms.sold_bot_raw = str(bot_raw)
        ms.sold_external_raw = str(ext_raw)
        if steps:
            updated += 1
            print(f"  {mint_addr[:12]} sold_bot_raw={bot_raw} sold_external_raw={ext_raw}")

    if args.dry_run:
        print("Dry-run: no state written.")
        return 0

    save_state_atomic(state_path, state)
    print(f"Wrote state to {state_path} (mints with executed_steps: {updated}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
