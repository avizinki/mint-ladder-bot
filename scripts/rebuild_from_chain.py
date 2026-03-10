#!/usr/bin/env python3
"""
Rebuild state from chain — Avizinki Master Execution Directive.

Fetches wallet transaction history (Helius), maps to buy/sell events, reconstructs
RuntimeState.mints with LotInfo per buy. Use after clean_runtime or when state is
lost. Mark lots as source=tx_parsed / entry_confidence=inferred when provenance
is from event stream only (no per-tx getTransaction).

Usage:
  python scripts/rebuild_from_chain.py [--state PATH] [--status PATH] [--wallet ADDR] [--archive-first]

Requires: HELIUS_API_KEY (or HELIUS_API_URL with api-key). Wallet from --wallet or status.json.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root so mint_ladder_bot is importable
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load .env from project root if present (HELIUS_API_KEY, etc.)
_env = _ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and k not in os.environ:
                os.environ[k] = v

from mint_ladder_bot.data.helius_client import get_wallet_transactions
from mint_ladder_bot.data.helius_adapter import map_helius_to_wallet_tx_events
from mint_ladder_bot.models import (
    BootstrapInfo,
    FailureInfo,
    LotInfo,
    RuntimeMintState,
    RuntimeState,
    SolBalance,
)
from mint_ladder_bot.state import save_state_atomic


def _load_wallet_from_status(status_path: Path) -> str | None:
    if not status_path.exists():
        return None
    try:
        data = json.loads(status_path.read_text())
        return (data.get("wallet") or "").strip() or None
    except Exception:
        return None


def _archive_first(state_path: Path, status_path: Path, events_path: Path, project_root: Path) -> None:
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_dir = project_root / "archive" / f"rebuild_{ts}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for p, name in [(state_path, "state.json"), (status_path, "status.json"), (events_path, "events.jsonl")]:
        if p.exists():
            shutil.copy2(p, archive_dir / name)
    print("Archived to", archive_dir)


def main() -> None:
    ap = argparse.ArgumentParser(description="Rebuild state from chain (Helius wallet history).")
    ap.add_argument("--state", type=Path, default=Path("state.json"), help="state.json path")
    ap.add_argument("--status", type=Path, default=Path("status.json"), help="status.json path")
    ap.add_argument("--wallet", type=str, default=None, help="Wallet pubkey (default: from status)")
    ap.add_argument("--archive-first", action="store_true", help="Archive state/status/events before rebuild")
    ap.add_argument("--project-root", type=Path, default=Path.cwd(), help="Project root")
    args = ap.parse_args()

    root = args.project_root.resolve()
    state_path = root / args.state if not args.state.is_absolute() else args.state
    status_path = root / args.status if not args.status.is_absolute() else args.status
    events_path = state_path.parent / "events.jsonl"

    wallet = (args.wallet or "").strip() or _load_wallet_from_status(status_path)
    if not wallet:
        print("No wallet. Set --wallet or provide status.json with wallet field.", file=sys.stderr)
        sys.exit(1)

    if args.archive_first:
        _archive_first(state_path, status_path, events_path, root)

    try:
        txs = get_wallet_transactions(wallet, limit=100)
    except Exception as e:
        print("Helius fetch failed:", e, file=sys.stderr)
        sys.exit(1)

    events = map_helius_to_wallet_tx_events(txs, wallet)
    buy_events = [e for e in events if e.type == "buy" and e.mint != "SOL" and e.token_delta > 0]

    # Build mints: one RuntimeMintState per mint, with one LotInfo per buy event
    mints: dict[str, RuntimeMintState] = {}
    decimals_default = 6
    for e in buy_events:
        mint = e.mint
        token_raw = e.token_delta
        sol_lamports = -e.sol_delta if e.sol_delta < 0 else 0
        sol_spent = sol_lamports / 1e9
        try:
            entry_price = sol_spent * (10 ** decimals_default) / float(token_raw)
        except ZeroDivisionError:
            entry_price = 0.0

        lot = LotInfo.create(
            mint=mint,
            token_amount_raw=token_raw,
            entry_price=entry_price,
            confidence="inferred",
            source="tx_parsed",
            entry_confidence="inferred",
            tx_signature=e.signature,
            swap_type="sol_to_token",
            program_or_venue="helius_rebuild",
        )
        lot.remaining_amount = str(token_raw)

        if mint not in mints:
            mints[mint] = RuntimeMintState(
                entry_price_sol_per_token=entry_price,
                entry_source="inferred_from_tx",
                original_entry_price_sol_per_token=entry_price,
                working_entry_price_sol_per_token=entry_price,
                trading_bag_raw=str(token_raw),
                moonbag_raw="0",
                bootstrap=BootstrapInfo(bootstrap_pending=False),
                executed_steps={},
                failures=FailureInfo(),
                lots=[],
            )
        mints[mint].lots.append(lot)
        # Aggregate trading_bag_raw for this mint (sum of remaining)
        total = sum(int(getattr(l, "remaining_amount", 0) or 0) for l in mints[mint].lots)
        mints[mint].trading_bag_raw = str(total)

    state = RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file=str(status_path),
        wallet=wallet,
        sol=None,
        mints=mints,
    )
    save_state_atomic(state_path, state)
    lots_total = sum(len(ms.lots) for ms in mints.values())
    print("Rebuild complete. mints=%d lots_total=%d" % (len(mints), lots_total))
    try:
        from mint_ladder_bot.integration.telegram_events import report
        report("DevOps", "Clean rebuild completed. mints=%d lots=%d." % (len(mints), lots_total))
    except Exception:
        pass


if __name__ == "__main__":
    main()
