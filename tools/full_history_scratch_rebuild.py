#!/usr/bin/env python3
"""
Full-history scratch rebuild using merged wallet + token-account history.

1. Fetches wallet history until exhausted; fetches token-account history for target mint until exhausted.
2. Merges and sorts by slot/blockTime ascending (oldest first).
3. Builds state from status (empty lots), runs tx-first lot engine from sig list, then external sells from sig list.
4. Saves to state_full_history_rebuild.json (no live state mutation).
5. Optionally runs reconciliation on rebuild state and reports.

See docs/FULL_HISTORY_RECONSTRUCTION_DESIGN.md.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _load_env() -> None:
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

WALLET = os.environ.get("REBUILD_WALLET", "3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR")
TARGET_MINT = os.environ.get("REBUILD_MINT", "x95HN3DWvbfCBtTjGm587z8suK3ec6cwQwgZNLbWKyp")
DATA_DIR = _REPO / "runtime" / "projects" / "mint_ladder_bot"
STATE_PATH = DATA_DIR / "state.json"
REBUILD_STATE_PATH = DATA_DIR / "state_full_history_rebuild.json"
STATUS_PATH = DATA_DIR / "status.json"

SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PAGE_SIZE = 1000


def _fetch_all_signatures(rpc: Any, address: str) -> List[Dict[str, Any]]:
    """Paginate get_signatures_for_address until empty. Returns list of { signature, slot?, blockTime? }."""
    all_sigs: List[Dict[str, Any]] = []
    before: Optional[str] = None
    while True:
        batch = rpc.get_signatures_for_address(address, limit=PAGE_SIZE, before=before)
        if not batch:
            break
        for item in batch:
            all_sigs.append({
                "signature": item.get("signature"),
                "slot": item.get("slot"),
                "blockTime": item.get("blockTime"),
            })
        if len(batch) < PAGE_SIZE:
            break
        before = batch[-1].get("signature")
        if not before:
            break
    return all_sigs


def _get_token_account_for_mint(rpc: Any, wallet: str, mint: str) -> Optional[str]:
    """Return token account pubkey for (wallet, mint) or None."""
    client = rpc._client_for(rpc._primary)
    token_accounts: List[Dict[str, Any]] = []
    for program_id in (SPL_TOKEN, TOKEN_2022):
        try:
            token_accounts.extend(client.get_token_accounts_by_owner(wallet, program_id=program_id))
        except Exception:
            continue
    for item in token_accounts:
        try:
            account = item.get("account") or {}
            data = account.get("data") or {}
            parsed = data.get("parsed") or {}
            info = parsed.get("info") or {}
            if info.get("mint") == mint:
                return item.get("pubkey")
        except Exception:
            continue
    return None


def _build_merged_sorted_sig_list(
    rpc: Any,
    wallet: str,
    target_mint: str,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Build merged (wallet + token-account for target mint) signature list, sorted oldest-first.
    Returns (sorted_sig_list, token_account_found).
    """
    wallet_sigs = _fetch_all_signatures(rpc, wallet)
    token_account = _get_token_account_for_mint(rpc, wallet, target_mint)
    ta_sigs: List[Dict[str, Any]] = []
    if token_account:
        ta_sigs = _fetch_all_signatures(rpc, token_account)
    all_by_sig: Dict[str, Dict[str, Any]] = {}
    for s in wallet_sigs:
        sig = s.get("signature")
        if sig:
            all_by_sig[sig] = s
    for s in ta_sigs:
        sig = s.get("signature")
        if sig and sig not in all_by_sig:
            all_by_sig[sig] = s
    merged = list(all_by_sig.values())
    # Sort by (slot, blockTime) ascending; put missing at end
    def sort_key(x: Dict[str, Any]) -> Tuple[int, int]:
        slot = x.get("slot")
        block_time = x.get("blockTime")
        return (slot if slot is not None else 2**31 - 1, block_time if block_time is not None else 2**31 - 1)
    merged.sort(key=sort_key)
    return merged, token_account is not None


def main() -> int:
    from mint_ladder_bot.config import Config
    from mint_ladder_bot.models import RuntimeState
    from mint_ladder_bot.state import load_state, save_state_atomic, ensure_mint_state
    from mint_ladder_bot.backfill_rpc import BackfillRpcClient
    from mint_ladder_bot.tx_lot_engine import run_tx_first_lot_engine_from_sig_list
    from mint_ladder_bot.runner import _ingest_external_sells_from_sig_list, _ensure_sell_accounting_backfill, _trading_bag_from_lots
    from mint_ladder_bot.strategy import compute_trading_bag

    config = Config()
    wallet = WALLET
    target_mint = TARGET_MINT
    status_path = STATUS_PATH
    rebuild_path = REBUILD_STATE_PATH
    if not status_path.exists():
        print("status.json not found; run status command first", file=sys.stderr)
        return 1

    from mint_ladder_bot.models import StatusFile
    status_data = StatusFile.model_validate_json(status_path.read_text())
    wallet_pubkey = status_data.wallet
    decimals_by_mint = {m.mint: getattr(m, "decimals", 6) for m in status_data.mints}
    symbol_by_mint = {m.mint: (m.symbol or m.mint[:8]) for m in status_data.mints}
    event_journal_path = rebuild_path.parent / "events_full_history_rebuild.jsonl"
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

    print("Building merged (wallet + token-account) signature list...")
    sorted_sig_list, had_token_account = _build_merged_sorted_sig_list(rpc, wallet_pubkey, target_mint)
    print(f"  Merged signature count: {len(sorted_sig_list)} (token_account_used={had_token_account})")

    if not sorted_sig_list:
        print("No signatures to replay; aborting.", file=sys.stderr)
        rpc.close()
        return 1

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
    for ms in state_obj.mints.values():
        _ensure_sell_accounting_backfill(ms)

    print("Running tx-first lot engine from merged sig list (oldest-first)...")
    n_buys = run_tx_first_lot_engine_from_sig_list(
        state_obj,
        rpc,
        wallet_pubkey,
        sorted_sig_list,
        decimals_by_mint,
        journal_path=event_journal_path,
        symbol_by_mint=symbol_by_mint,
        delay_after_request_sec=0.0,
    )
    print(f"  Lots created: {n_buys}")

    print("Running external sell ingestion from same sig list...")
    n_sells = _ingest_external_sells_from_sig_list(
        state_obj,
        rpc,
        wallet_pubkey,
        sorted_sig_list,
        journal_path=event_journal_path,
    )
    print(f"  External sells ingested: {n_sells}")

    for ms in state_obj.mints.values():
        ms.trading_bag_raw = str(_trading_bag_from_lots(ms))

    rebuild_path.parent.mkdir(parents=True, exist_ok=True)
    save_state_atomic(rebuild_path, state_obj)
    rpc.close()
    print(f"Rebuild state saved to {rebuild_path}")

    # Reconciliation report on rebuild state
    try:
        from mint_ladder_bot.reconciliation_report import compute_reconciliation_records
        records = compute_reconciliation_records(state_obj, status_data, mint_filter=target_mint)
        print("\nReconciliation (rebuild state):")
        for r in records:
            print(f"  {r.mint[:12]} diff_pct={r.diff_pct} status={r.reconciliation_status}")
    except Exception as e:
        print(f"\nReconciliation check skipped: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
