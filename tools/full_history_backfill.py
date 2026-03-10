#!/usr/bin/env python3
"""
Full wallet history backfill — no signature cap. Paginate until provider exhausted.
Used for $HACHI reconciliation fix. Read state (or build from status), run tx-first
+ external sells with max_signatures=50000, save state.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from datetime import datetime, timezone

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

def _load_env():
    for p in (_REPO / ".env", Path(".env")):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()

_load_env()

WALLET = "3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR"
DATA_DIR = _REPO / "runtime" / "projects" / "mint_ladder_bot"
STATE_PATH = DATA_DIR / "state.json"
STATUS_PATH = DATA_DIR / "status.json"

# No cap: scan until RPC returns no more pages
MAX_SIGNATURES = 50000


def main() -> None:
    from mint_ladder_bot.config import Config
    from mint_ladder_bot.models import StatusFile, RuntimeState
    from mint_ladder_bot.state import load_state, save_state_atomic
    from mint_ladder_bot.backfill_rpc import BackfillRpcClient
    from mint_ladder_bot.tx_lot_engine import run_tx_first_lot_engine
    from mint_ladder_bot.runner import _ingest_external_sells, _ensure_sell_accounting_backfill, _trading_bag_from_lots
    from mint_ladder_bot.strategy import compute_trading_bag
    from mint_ladder_bot.state import ensure_mint_state

    config = Config()
    status_path = STATUS_PATH
    state_path = STATE_PATH
    if not status_path.exists():
        print("status.json not found; run status command first")
        sys.exit(1)

    status_data = StatusFile.model_validate_json(status_path.read_text())
    wallet_pubkey = status_data.wallet
    decimals_by_mint = {m.mint: getattr(m, "decimals", 6) for m in status_data.mints}
    symbol_by_mint = {m.mint: (m.symbol or m.mint[:8]) for m in status_data.mints}
    event_journal_path = state_path.parent / "events.jsonl"
    delay_sec = max(0.0, min(int(os.environ.get("TX_BACKFILL_DELAY_MS", "200")) / 1000.0, 2.0))
    primary = (os.environ.get("RPC_PRIMARY") or "").strip() or config.rpc_endpoint
    pool_list = [u.strip() for u in (os.environ.get("RPC_BACKFILL_POOL") or "").strip().split(",") if u.strip()]
    rpc = BackfillRpcClient(
        primary_endpoint=primary,
        pool_endpoints=pool_list,
        timeout_s=getattr(config, "rpc_timeout_s", 20.0),
        delay_after_request_sec=delay_sec,
        max_retries_per_endpoint=2,
    )

    if state_path.exists():
        state_obj = load_state(state_path, status_path)
        if not state_obj.mints:
            print("state has no mints; run bot once with CLEAN_START=1 or use --init-from-status")
            rpc.close()
            sys.exit(1)
        print("Loaded existing state; extending with full history (max_signatures=%s)..." % MAX_SIGNATURES)
    else:
        # Build state from status (empty lots)
        state_obj = RuntimeState(
            version=1,
            started_at=datetime.now(tz=timezone.utc),
            status_file=str(status_path),
            wallet=status_data.wallet,
            sol=status_data.sol,
            mints={},
        )
        for m in status_data.mints:
            balance_raw = int(getattr(m, "balance_raw", 0) or 0)
            entry = getattr(m, "entry", None)
            ep = entry.entry_price_sol_per_token if entry else 0.0
            src = getattr(entry, "entry_source", None) if entry else None
            trading_bag_raw, moonbag_raw = compute_trading_bag(str(balance_raw), config.trading_bag_pct)
            ensure_mint_state(
                state_obj,
                m.mint,
                entry_price_sol_per_token=ep,
                trading_bag_raw=trading_bag_raw,
                moonbag_raw=moonbag_raw,
                entry_source=src if src and src != "unknown" else None,
            )
        print("Built state from status; running full history (max_signatures=%s)..." % MAX_SIGNATURES)

    for ms in state_obj.mints.values():
        _ensure_sell_accounting_backfill(ms)

    n_buys = run_tx_first_lot_engine(
        state_obj,
        rpc,
        wallet_pubkey,
        decimals_by_mint,
        journal_path=event_journal_path,
        max_signatures=MAX_SIGNATURES,
        symbol_by_mint=symbol_by_mint,
        delay_after_request_sec=0.0,
    )
    print("Tx-first: %d lots created" % n_buys)
    n_sells = _ingest_external_sells(
        state_obj,
        rpc,
        wallet_pubkey,
        max_signatures=MAX_SIGNATURES,
        journal_path=event_journal_path,
        config=config,
    )
    print("External sells ingested: %d" % n_sells)
    for ms in state_obj.mints.values():
        ms.trading_bag_raw = str(_trading_bag_from_lots(ms))
    save_state_atomic(state_path, state_obj)
    rpc.close()
    print("State saved to %s" % state_path)


if __name__ == "__main__":
    main()
