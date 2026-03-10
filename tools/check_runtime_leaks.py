#!/usr/bin/env python3
"""
Guard script: fail if runtime artifacts exist outside the centralized runtime/ tree.

Intended to be run from repo root or mint-ladder-bot project root.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List


CANONICAL_NAMES = {
    "state.json",
    "status.json",
    "events.jsonl",
    "run.log",
    "health_status.json",
    "safety_state.json",
}

PATTERN_SUFFIXES = (".bak", ".bak.1", ".bak.2", ".bak.3", ".wav")


def _is_under_runtime(path: Path, runtime_root: Path) -> bool:
    try:
        path.relative_to(runtime_root)
        return True
    except ValueError:
        return False


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    runtime_root = repo_root / "runtime"

    offending: List[Path] = []
    for p in repo_root.rglob("*"):
        if not p.is_file():
            continue
        # allow anything under runtime/
        if _is_under_runtime(p, runtime_root):
            continue
        name = p.name
        if name in CANONICAL_NAMES:
            offending.append(p)
            continue
        if name.startswith(".monitor_"):
            offending.append(p)
            continue
        if any(name.endswith(sfx) for sfx in PATTERN_SUFFIXES):
            offending.append(p)
    # Explicit directory guards for architecture rule: no runtime-related
    # directories under mint-ladder-bot/ (logs/, runtime/, etc.).
    dir_offenders: List[Path] = []
    project_root = repo_root / "mint-ladder-bot"
    for dname in ("logs", "runtime"):
        dpath = project_root / dname
        if dpath.is_dir():
            dir_offenders.append(dpath)

    if offending or dir_offenders:
        print("RUNTIME LEAKS DETECTED:", file=sys.stderr)
        for p in sorted(offending + dir_offenders):
            print(str(p), file=sys.stderr)
        return 1

    print("No runtime leaks detected (outside runtime/).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

