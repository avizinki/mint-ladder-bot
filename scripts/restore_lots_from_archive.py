#!/usr/bin/env python3
"""
Restore missing lots from an archived state.json into current state.json.
CEO DIRECTIVE: Do NOT merge old records back into live state. This script is disabled by default.
Only run if RESTORE_LOTS_FROM_ARCHIVE_ALLOWED=1 (founder approval).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def main() -> int:
    if os.getenv("RESTORE_LOTS_FROM_ARCHIVE_ALLOWED", "").strip() != "1":
        print("CEO directive: do not merge archived state into live state. Exiting.", file=sys.stderr)
        print("Set RESTORE_LOTS_FROM_ARCHIVE_ALLOWED=1 only with founder approval.", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parent.parent
    current_path = root / "state.json"
    archive_path = root / "archive" / "full_cleanup_20260308_0311" / "state.json"

    if not archive_path.exists():
        print(f"Archive not found: {archive_path}", file=sys.stderr)
        return 1
    if not current_path.exists():
        print(f"Current state not found: {current_path}", file=sys.stderr)
        return 1

    current = json.loads(current_path.read_text())
    archive = json.loads(archive_path.read_text())

    current_mints = current.get("mints") or {}
    archive_mints = archive.get("mints") or {}
    replaced_mints = 0
    total_restored = 0
    for mint, arch_md in archive_mints.items():
        if mint not in current_mints:
            continue
        arch_lots = arch_md.get("lots") or []
        cur_md = current_mints[mint]
        prev_count = len(cur_md.get("lots") or [])
        cur_md["lots"] = list(arch_lots)
        if arch_lots:
            replaced_mints += 1
            total_restored += len(arch_lots) - prev_count

    if replaced_mints == 0:
        print("No mints to restore (archive has no matching mints).")
        return 0

    # Backup and write
    bak = current_path.with_suffix(current_path.suffix + ".bak.restore_pre")
    shutil.copy2(current_path, bak)
    tmp = current_path.with_suffix(current_path.suffix + ".tmp")
    tmp.write_text(json.dumps(current, indent=2))
    tmp.replace(current_path)
    total_now = sum(len((m or {}).get("lots") or []) for m in current_mints.values())
    print(f"Restored lots from archive: {replaced_mints} mints, {total_now} total lots. Backup: {bak.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
