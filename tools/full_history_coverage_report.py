#!/usr/bin/env python3
"""
Read-only full-history coverage tool.

Reports for a wallet (and optional target mint):
- Earliest/latest wallet tx timestamp and total wallet tx count
- Token accounts discovered for the wallet; for target mint: token account and its tx range
- Whether each source exhausted naturally (empty page)
- Combined earliest timestamp for the mint

No state mutation. Proves whether we have early history coverage.
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

# Defaults: $HACHI and wallet from CEO directive
WALLET = os.environ.get("COVERAGE_WALLET", "3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR")
HACHI_MINT = os.environ.get("COVERAGE_MINT", "x95HN3DWvbfCBtTjGm587z8suK3ec6cwQwgZNLbWKyp")

SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

PAGE_SIZE = 1000


def _ts_display(ts: Optional[int]) -> str:
    if ts is None:
        return "N/A"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return f"{dt.isoformat()} (slot/blockTime={ts})"
    except Exception:
        return str(ts)


def _fetch_all_signatures(
    rpc: Any,
    address: str,
    label: str,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Paginate get_signatures_for_address until empty. Returns (list of sig infos, exhausted).
    Each item: { signature, slot?, blockTime? }.
    """
    all_sigs: List[Dict[str, Any]] = []
    before: Optional[str] = None
    exhausted = False
    while True:
        batch = rpc.get_signatures_for_address(address, limit=PAGE_SIZE, before=before)
        if not batch:
            exhausted = True
            break
        for item in batch:
            all_sigs.append(
                {
                    "signature": item.get("signature"),
                    "slot": item.get("slot"),
                    "blockTime": item.get("blockTime"),
                }
            )
        if len(batch) < PAGE_SIZE:
            exhausted = True
            break
        before = batch[-1].get("signature")
        if not before:
            exhausted = True
            break
    return all_sigs, exhausted


def _get_token_account_for_mint(
    rpc: Any,
    wallet: str,
    mint: str,
) -> Optional[str]:
    """Return token account pubkey for (wallet, mint) or None."""
    # BackfillRpcClient delegates to RpcClient per endpoint; use primary for read-only token accounts
    client = rpc._client_for(rpc._primary)
    token_accounts: List[Dict[str, Any]] = []
    for program_id in (SPL_TOKEN, TOKEN_2022):
        try:
            token_accounts.extend(client.get_token_accounts_by_owner(wallet, program_id=program_id))
        except Exception:
            continue
    for item in token_accounts:
        try:
            pubkey = item.get("pubkey")
            account = item.get("account") or {}
            data = account.get("data") or {}
            parsed = data.get("parsed") or {}
            info = parsed.get("info") or {}
            if info.get("mint") == mint:
                return pubkey
        except Exception:
            continue
    return None


def main() -> int:
    from mint_ladder_bot.config import Config
    from mint_ladder_bot.backfill_rpc import BackfillRpcClient

    config = Config()
    wallet = WALLET
    target_mint = HACHI_MINT
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

    out_lines: List[str] = []
    def out(s: str) -> None:
        out_lines.append(s)
        print(s)

    out("=== Full-history coverage report (read-only) ===")
    out(f"Wallet: {wallet}")
    out(f"Target mint: {target_mint}")
    out("")

    # 1) Wallet history until exhausted
    out("Fetching wallet signature history (paginate until empty)...")
    wallet_sigs, wallet_exhausted = _fetch_all_signatures(rpc, wallet, "wallet")
    out(f"  Wallet tx count: {len(wallet_sigs)}")
    out(f"  Wallet exhausted (empty page): {wallet_exhausted}")

    if wallet_sigs:
        # getSignaturesForAddress returns newest first; we want earliest = last in list
        by_slot = [s for s in wallet_sigs if s.get("slot") is not None]
        by_block_time = [s for s in wallet_sigs if s.get("blockTime") is not None]
        if by_slot:
            earliest_slot = min(s["slot"] for s in by_slot)
            latest_slot = max(s["slot"] for s in by_slot)
            out(f"  Earliest wallet tx (slot): {earliest_slot}")
            out(f"  Latest wallet tx (slot): {latest_slot}")
        if by_block_time:
            earliest_ts = min(s["blockTime"] for s in by_block_time)
            latest_ts = max(s["blockTime"] for s in by_block_time)
            out(f"  Earliest wallet tx (blockTime): {_ts_display(earliest_ts)}")
            out(f"  Latest wallet tx (blockTime): {_ts_display(latest_ts)}")
    else:
        earliest_wallet_ts = None
        latest_wallet_ts = None
    out("")

    # 2) Token account for target mint
    ta_sigs = []
    hachi_earliest_ts = None
    hachi_latest_ts = None
    hachi_tx_count = 0
    token_account_exhausted = False

    out(f"Resolving token account for mint {target_mint[:16]}...")
    token_account = _get_token_account_for_mint(rpc, wallet, target_mint)
    if not token_account:
        out("  No token account found for this mint (wallet may have no position or different program).")
    else:
        out(f"  Token account: {token_account}")
        out("Fetching token-account signature history (paginate until empty)...")
        ta_sigs, token_account_exhausted = _fetch_all_signatures(rpc, token_account, "token_account")
        hachi_tx_count = len(ta_sigs)
        out(f"  Token-account tx count: {hachi_tx_count}")
        out(f"  Token-account exhausted: {token_account_exhausted}")
        by_bt = [s for s in ta_sigs if s.get("blockTime") is not None]
        hachi_earliest_ts = min(s["blockTime"] for s in by_bt) if by_bt else None
        hachi_latest_ts = max(s["blockTime"] for s in by_bt) if by_bt else None
        if hachi_earliest_ts is not None:
            out(f"  Earliest $HACHI tx (blockTime): {_ts_display(hachi_earliest_ts)}")
        if hachi_latest_ts is not None:
            out(f"  Latest $HACHI tx (blockTime): {_ts_display(hachi_latest_ts)}")
    out("")

    # 3) Combined / merged view (wallet + token-account, dedupe by signature)
    all_sigs_by_sig = {}
    for s in wallet_sigs:
        sig = s.get("signature")
        if sig:
            all_sigs_by_sig[sig] = s
    for s in ta_sigs:
        sig = s.get("signature")
        if sig and sig not in all_sigs_by_sig:
            all_sigs_by_sig[sig] = s
    merged = list(all_sigs_by_sig.values())
    merged_with_time = [m for m in merged if m.get("blockTime") is not None]
    combined_earliest = min(m["blockTime"] for m in merged_with_time) if merged_with_time else None
    combined_latest = max(m["blockTime"] for m in merged_with_time) if merged_with_time else None

    out("Merged (wallet + token-account, deduped by signature):")
    out(f"  Merged signature count: {len(merged)}")
    out(f"  Combined earliest timestamp: {_ts_display(combined_earliest)}")
    out(f"  Combined latest timestamp: {_ts_display(combined_latest)}")
    out("")

    # 4) For target mint summary
    out("Target mint ($HACHI) coverage summary:")
    out(f"  Earliest $HACHI tx reached: {_ts_display(hachi_earliest_ts if token_account else None)}")
    out(f"  Latest $HACHI tx reached: {_ts_display(hachi_latest_ts if token_account else None)}")
    out(f"  $HACHI tx count (token-account): {hachi_tx_count if token_account else 0}")
    out(f"  Wallet exhausted: {wallet_exhausted}")
    out(f"  Token-account exhausted: {token_account_exhausted if token_account else 'N/A'}")
    out("")

    # 5) Risks / incomplete
    if not wallet_exhausted and wallet_sigs:
        out("Note: Wallet pagination did not exhaust (last page was full). Coverage may be incomplete.")
    if token_account and not token_account_exhausted and hachi_tx_count:
        out("Note: Token-account pagination did not exhaust. $HACHI coverage may be incomplete.")
    out("Done.")

    # Optionally write to file
    report_path = _REPO / "runtime" / "projects" / "mint_ladder_bot" / "full_history_coverage_report.txt"
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        print(f"\nReport also written to: {report_path}", file=sys.stderr)
    except Exception as e:
        print(f"\nCould not write report file: {e}", file=sys.stderr)

    rpc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
