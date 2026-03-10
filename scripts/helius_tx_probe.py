#!/usr/bin/env python3
"""
Helius TX Probe — CEO directive: pilot validation only.

Pulls last 50 transactions for the trading wallet via Helius Enhanced
Transactions API, extracts signature / mint / token_delta / sol_delta /
timestamp, and prints BUY/SELL/TRANSFER lines.

Goal: Confirm Helius provides enough data to rebuild lots deterministically.
No integration with ledger yet. Validation only.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Project root
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
    api_key = (os.environ.get("HELIUS_API_KEY") or "").strip()
    if not api_key:
        print("HELIUS_API_KEY missing. Set in .env or environment.", file=sys.stderr)
        sys.exit(1)

    # Wallet: .env WALLET, or status.json if present
    wallet = (os.environ.get("WALLET") or "").strip()
    status_path = ROOT / "status.json"
    if not wallet and status_path.exists():
        data = json.loads(status_path.read_text())
        wallet = (data.get("wallet") or "").strip()
    if not wallet:
        print("WALLET missing. Set WALLET in .env or ensure status.json contains wallet.", file=sys.stderr)
        sys.exit(1)

    # Symbol map (mint -> symbol) for display
    symbol_by_mint: dict[str, str] = {}
    if status_path.exists():
        data = json.loads(status_path.read_text())
        for m in (data.get("mints") or []):
            if isinstance(m, dict) and m.get("mint"):
                symbol_by_mint[m["mint"]] = (m.get("symbol") or m["mint"][:8])

    from mint_ladder_bot.data.helius_adapter import get_wallet_tx_events

    # Do not print wallet or API key (Security: read-only probe, no leaks)
    print("Fetching last 50 transactions (Helius Enhanced Transactions API)...")
    events = get_wallet_tx_events(address=wallet, api_key=api_key, limit=50)
    print(f"Events: {len(events)}")
    print()

    for e in events:
        if e.mint == "SOL":
            sol_sol = e.sol_delta / 1e9
            sol_str = f"{sol_sol:+.4f}" if sol_sol != 0 else "0"
            print(f"TRANSFER SOL {sol_str} SOL")
            continue
        symbol = symbol_by_mint.get(e.mint, e.mint[:8])
        tok_str = f"+{e.token_delta}" if e.token_delta >= 0 else str(e.token_delta)
        sol_sol = e.sol_delta / 1e9
        sol_str = f"{sol_sol:+.4f}" if sol_sol != 0 else "0"
        print(f"{e.type.upper():6}  {symbol:12}  {tok_str:>14} tokens   {sol_str:>10} SOL")

    print()
    print("Pilot complete. If BUY/SELL deltas match on-chain history, Helius can replace tx parsing.")


if __name__ == "__main__":
    main()
