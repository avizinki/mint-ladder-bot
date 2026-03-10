#!/usr/bin/env python3
"""
Clean runtime — hard operational reset per Avizinki Master Execution Directive.

Stops all mint-ladder processes, deletes runtime files (state, status, run.log,
events, safety_state, health_status, etc.), recreates directories. Does NOT
rebuild from chain; use rebuild_from_chain.py for that after clean.

Usage:
  python scripts/clean_runtime.py [--project-root DIR] [--yes]

Without --yes, prints what would be done and exits. With --yes, performs the reset.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path


RUNTIME_FILES = [
    "state.json",
    "status.json",
    "run.log",
    "events.jsonl",
    "safety_state.json",
    "health_status.json",
    "uptime_alerts.jsonl",
    "escalation.jsonl",
    "restart_log.jsonl",
    "alerts.json",
    ".lane_state.json",
]
RUNTIME_DIRS = ["runtime_archive"]


def _project_root(path: Path) -> Path:
    p = path.resolve()
    if not p.is_dir():
        p = p.parent
    return p


def find_mint_ladder_pids() -> list[int]:
    """Return list of PIDs for mint_ladder / mint-ladder processes."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "mint_ladder|mint-ladder"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0 and not out.stdout.strip():
            return []
        return [int(x) for x in out.stdout.strip().split() if x.strip().isdigit()]
    except Exception:
        return []


def stop_processes() -> list[int]:
    """Send SIGTERM to mint-ladder processes; return PIDs that were sent."""
    pids = find_mint_ladder_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            pass
    return pids


def main() -> None:
    ap = argparse.ArgumentParser(description="Clean runtime: stop processes, delete runtime files.")
    ap.add_argument("--project-root", type=Path, default=Path.cwd(), help="Project root (default: cwd)")
    ap.add_argument("--yes", action="store_true", help="Actually perform reset")
    args = ap.parse_args()
    # In the new architecture, runtime artifacts live under the centralized
    # runtime/ tree, not under the project root. This script now only operates
    # on files in the project root that may still exist from legacy runs, and
    # on the top-level runtime_archive under the repo root.
    root = _project_root(args.project_root)

    pids = find_mint_ladder_pids()
    to_remove = []
    for name in RUNTIME_FILES:
        f = root / name
        if f.exists():
            to_remove.append(f)

    print("Clean runtime (hard reset, legacy files only)")
    print("  Project root (legacy scope):", root)
    print("  Processes to stop:", pids or "none")
    print("  Files to remove:", [str(p.relative_to(root)) for p in to_remove] or "none")

    if not args.yes:
        print("Run with --yes to perform reset.")
        sys.exit(0)

    if pids:
        stop_processes()
        import time
        time.sleep(2)
        remaining = find_mint_ladder_pids()
        if remaining:
            print("Warning: some processes may still be running:", remaining)

    for f in to_remove:
        try:
            f.unlink()
            print("  Removed:", f.name)
        except Exception as e:
            print("  Failed to remove", f.name, ":", e)

    repo_root = root.parent
    for d in RUNTIME_DIRS:
        dpath = repo_root / d
        if dpath.exists():
            try:
                import shutil
                shutil.rmtree(dpath)
                print("  Removed dir:", d)
            except Exception as e:
                print("  Failed to remove dir", d, ":", e)
        try:
            dpath.mkdir(parents=True, exist_ok=True)
            print("  Recreated:", d)
        except Exception as e:
            print("  Failed to recreate", d, ":", e)

    print("Done. Next: restore or generate status.json if needed, then start bot.")
    print("Optional: python scripts/rebuild_from_chain.py --archive-first ...")


if __name__ == "__main__":
    main()
