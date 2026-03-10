#!/usr/bin/env python3
"""
Validate that state.json, /runtime/dashboard API, and display-pending logic are aligned.
Uses mint_ladder_bot.dashboard_truth as single source for pending count (no duplicate logic).
Usage: python tools/validate_dashboard_alignment.py [--base-url http://127.0.0.1:8765]
Exit 0 if aligned, 1 if mismatch.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Use shared truth layer (same as dashboard_server)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mint_ladder_bot import dashboard_truth as dt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:6200", help="Dashboard base URL")
    ap.add_argument("--state-path", type=Path, default=None, help="Path to state.json (default: same dir as script/../state.json)")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    state_path = args.state_path or (Path(__file__).resolve().parent.parent / "state.json")

    # 1) State-derived pending count from single truth layer
    state_pending = 0
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text())
            state_pending = dt.pending_lots_count_from_state(data)
        except Exception as e:
            print(f"state.json read failed: {e}", file=sys.stderr)
            return 1
    else:
        print("state.json not found", file=sys.stderr)
        return 1

    # 2) API pending count
    try:
        import urllib.request
        req = urllib.request.Request(f"{base}/runtime/dashboard", headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode())
    except Exception as e:
        print(f"API request failed: {e}", file=sys.stderr)
        return 1

    api_pending = int(payload.get("pending_lots_count") or 0)
    recent_buys = payload.get("recent_buys") or []
    api_pending_rows = sum(1 for r in recent_buys if r.get("entry_confidence") == "pending_price_resolution")

    ok = state_pending == api_pending == api_pending_rows
    print(f"state pending count:   {state_pending}")
    print(f"API pending_lots_count: {api_pending}")
    print(f"API recent_buys (pending): {api_pending_rows}")
    print(f"alignment: {'yes' if ok else 'NO - MISMATCH'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
