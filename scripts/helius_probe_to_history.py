#!/usr/bin/env python3
"""
Helius probe → wallet_trade_history.json — Analytics (Phase 1).

Read-only: fetches last 50 transactions via Helius adapter, writes normalized
events to wallet_trade_history.json for visual inspection. Does not run the
probe script; uses same adapter so output is identical in structure.

Usage:
  python scripts/helius_probe_to_history.py [--limit 50] [--out wallet_trade_history.json]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Helius probe → wallet_trade_history.json")
    p.add_argument("--limit", type=int, default=50, help="Max transactions to fetch")
    p.add_argument("--out", default="wallet_trade_history.json", help="Output JSON path")
    args = p.parse_args()

    api_key = (os.environ.get("HELIUS_API_KEY") or "").strip()
    if not api_key:
        print("HELIUS_API_KEY missing.", file=sys.stderr)
        sys.exit(1)

    wallet = (os.environ.get("WALLET") or "").strip()
    status_path = ROOT / "status.json"
    if not wallet and status_path.exists():
        wallet = (json.loads(status_path.read_text()).get("wallet") or "").strip()
    if not wallet:
        print("WALLET missing (set in .env or status.json).", file=sys.stderr)
        sys.exit(1)

    from mint_ladder_bot.data.helius_adapter import get_wallet_tx_events

    events = get_wallet_tx_events(address=wallet, api_key=api_key, limit=args.limit)

    # Normalize for JSON: datetime → iso string, keep raw deltas
    out_list = []
    for e in events:
        out_list.append({
            "signature": e.signature,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            "timestamp_ts": e.timestamp_ts,
            "mint": e.mint,
            "token_delta": e.token_delta,
            "sol_delta": e.sol_delta,
            "type": e.type,
        })

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.write_text(json.dumps(out_list, indent=2))

    print(f"Wrote {len(out_list)} events to {out_path}")


if __name__ == "__main__":
    main()
