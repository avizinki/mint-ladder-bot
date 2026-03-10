#!/usr/bin/env python3
"""
Clear all lots from state.json so the next runner start rebuilds from tx history only.
Use after archiving state/status/run.log/events per docs/CLEAN_REBUILD_PLAYBOOK.md.
Keeps mints and other state; only sets lots = [] and optionally clears last_known_balance_raw.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    state_path = root / "state.json"
    if not state_path.exists():
        print("state.json not found", file=sys.stderr)
        return 1
    data = json.loads(state_path.read_text())
    mints = data.get("mints") or {}
    cleared = 0
    for mint, ms in mints.items():
        if isinstance(ms, dict) and "lots" in ms:
            if ms.get("lots"):
                ms["lots"] = []
                if "last_known_balance_raw" in ms:
                    ms["last_known_balance_raw"] = None
                cleared += 1
    state_path.write_text(json.dumps(data, indent=2))
    print(f"Cleared lots for {cleared} mints. Next run will run tx-first then migration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
