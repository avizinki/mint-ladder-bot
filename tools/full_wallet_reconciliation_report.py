#!/usr/bin/env python3
"""
Wallet-wide full-history reconciliation report (read-only).

For a single wallet, this tool:
- Loads current status + runtime state (if present) as the "before" view.
- Builds full-history scratch reconstructions for all mints currently in the wallet
  using merged wallet + token-account history (oldest-first).
- Computes reconciliation metrics before vs after for each mint.
- Classifies each mint into resume categories (A–E) and writes:
  - A machine-readable JSON report.
  - A human-readable markdown report.

Constraints:
- No mutation of live runtime state (state.json is never written).
- All scratch work is in-memory; only reports are written.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
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

# Default wallet from CEO directive; can be overridden via env.
WALLET = os.environ.get(
    "FULL_WALLET_RECON_WALLET",
    "3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR",
)

DATA_DIR = _REPO / "runtime" / "projects" / "mint_ladder_bot"
STATE_PATH = DATA_DIR / "state.json"
STATUS_PATH = DATA_DIR / "status.json"
JSON_REPORT_PATH = DATA_DIR / "full_wallet_reconciliation_report.json"
MD_REPORT_PATH = DATA_DIR / "full_wallet_reconciliation_report.md"

SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PAGE_SIZE = 1000


def _fetch_all_signatures(
    rpc: Any,
    address: str,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Paginate get_signatures_for_address until empty.

    Returns (list of sig infos, exhausted).
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
    client = rpc._client_for(rpc._primary)
    token_accounts: List[Dict[str, Any]] = []
    for program_id in (SPL_TOKEN, TOKEN_2022):
        try:
            token_accounts.extend(
                client.get_token_accounts_by_owner(wallet, program_id=program_id)
            )
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


def _merge_and_sort_sigs(
    wallet_sigs: List[Dict[str, Any]],
    ta_sigs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Merge wallet + token-account sig lists, dedupe by signature, sort oldest-first.
    """
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

    def sort_key(x: Dict[str, Any]) -> Tuple[int, int]:
        slot = x.get("slot")
        block_time = x.get("blockTime")
        return (
            slot if slot is not None else 2**31 - 1,
            block_time if block_time is not None else 2**31 - 1,
        )

    merged.sort(key=sort_key)
    return merged


def _ts_display(ts: Optional[int]) -> str:
    if ts is None:
        return "N/A"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.isoformat()
    except Exception:
        return str(ts)


@dataclass
class PerMintReconciliationSnapshot:
    symbol: Optional[str]
    mint: str
    wallet_balance_raw: int
    sum_active_lots_raw_before: int
    sum_active_lots_raw_after: int
    diff_pct_before: Optional[float]
    diff_pct_after: Optional[float]
    reconciliation_status_before: Optional[str]
    reconciliation_status_after: Optional[str]
    blocker_category_before: Optional[str]
    blocker_category_after: Optional[str]
    earliest_wallet_tx_for_mint: Optional[str]
    earliest_token_account_tx_for_mint: Optional[str]
    earliest_lot_after_rebuild: Optional[str]
    provider_exhausted_wallet: bool
    provider_exhausted_token_account: Optional[bool]
    dominant_blocker_category: Optional[str]
    resume_classification: str


def _classify_resume_category(
    wallet_balance_raw: int,
    diff_pct_after: Optional[float],
    reconciliation_status_after: Optional[str],
    blocker_category_after: Optional[str],
    provider_exhausted_wallet: bool,
    provider_exhausted_token_account: Optional[bool],
    bag_zero_reason_after: Optional[str],
) -> str:
    """
    Map per-mint reconciliation outcome into A–E categories.

    A. SAFE TO RESUME
    B. SAFE TO RESUME WITH MANUAL OVERRIDE
    C. NOT SAFE — SOURCE GAP
    D. NOT SAFE — PARSER / TX SHAPE GAP
    E. IGNORE / NON-TRADABLE LEFTOVER
    """
    status = (reconciliation_status_after or "").lower()
    blocker = (blocker_category_after or "").lower()
    exhausted_wallet = bool(provider_exhausted_wallet)
    exhausted_ta = provider_exhausted_token_account
    abs_diff = abs(diff_pct_after) if diff_pct_after is not None else None

    # E: Ignore / non-tradable leftover (dust / bootstrap-only / explicit dust reasons).
    if wallet_balance_raw <= 0:
        return "E_IGNORE_NON_TRADABLE_LEFTOVER"
    if bag_zero_reason_after:
        r = bag_zero_reason_after.lower()
        if "dust" in r or "bootstrap" in r or "non_tradable" in r:
            return "E_IGNORE_NON_TRADABLE_LEFTOVER"
    if blocker in ("bootstrap-only inventory",) and status == "sufficient":
        return "E_IGNORE_NON_TRADABLE_LEFTOVER"

    # A: Safe to resume — sufficient reconciliation and no high-risk blocker.
    if status == "sufficient":
        if blocker not in (
            "missing historical tx coverage",
            "wallet transfer / unknown provenance",
        ):
            return "A_SAFE_TO_RESUME"
        # If residual blocker is "missing coverage" but diff is tiny, still treat as A.
        if abs_diff is not None and abs_diff <= 0.02:
            return "A_SAFE_TO_RESUME"

    # B: Safe to resume with manual override — partial reconciliation but controlled.
    if status == "partial":
        if abs_diff is not None and abs_diff <= 0.25:
            # History looks partially incomplete, but mismatch is bounded.
            return "B_SAFE_TO_RESUME_WITH_MANUAL_OVERRIDE"

    # C: Not safe — source gap (history still incomplete / provenance missing).
    if blocker in (
        "missing historical tx coverage",
        "wallet transfer / unknown provenance",
    ):
        # Wallet or token-account pagination not exhausted implies potential deeper history.
        if not exhausted_wallet or (exhausted_ta is False):
            return "C_NOT_SAFE_SOURCE_GAP"
        # Even if exhausted, large residual suggests structural source gap.
        if abs_diff is None or abs_diff > 0.25:
            return "C_NOT_SAFE_SOURCE_GAP"

    # D: Not safe — parser / tx shape gap.
    # Remaining non-sufficient cases with exhausted history likely indicate parsing/shape issues.
    if exhausted_wallet and (exhausted_ta is True or exhausted_ta is None):
        return "D_NOT_SAFE_PARSER_TX_SHAPE_GAP"

    # Fallback: treat unknown as conservative source gap.
    return "C_NOT_SAFE_SOURCE_GAP"


def main() -> int:
    from mint_ladder_bot.config import Config
    import mint_ladder_bot.runner as runner_mod
    from mint_ladder_bot.models import RuntimeState, StatusFile
    from mint_ladder_bot.reconciliation_report import compute_reconciliation_records
    from mint_ladder_bot.state import ensure_mint_state, load_state
    from mint_ladder_bot.strategy import compute_trading_bag
    from mint_ladder_bot.backfill_rpc import BackfillRpcClient
    from mint_ladder_bot.tx_lot_engine import run_tx_first_lot_engine_from_sig_list
    from mint_ladder_bot.runner import (
        _ensure_sell_accounting_backfill,
        _ingest_external_sells_from_sig_list,
        _trading_bag_from_lots,
    )

    if not STATUS_PATH.exists():
        print("status.json not found; run status command first", file=sys.stderr)
        return 1

    config = Config()
    # Ensure runner module has a global config for helpers that expect it.
    setattr(runner_mod, "config", config)
    status_data = StatusFile.model_validate_json(STATUS_PATH.read_text())
    wallet_pubkey = status_data.wallet

    # Sanity: CEO directive wallet vs status wallet.
    if WALLET and WALLET != wallet_pubkey:
        print(
            f"Warning: FULL_WALLET_RECON_WALLET={WALLET} differs from status wallet={wallet_pubkey}",
            file=sys.stderr,
        )

    decimals_by_mint: Dict[str, int] = {
        m.mint: getattr(m, "decimals", 6) for m in status_data.mints
    }
    symbol_by_mint: Dict[str, str] = {
        m.mint: (m.symbol or m.mint[:8]) for m in status_data.mints
    }

    # Load baseline runtime state if present; otherwise build from status snapshot.
    if STATE_PATH.exists():
        state_before = load_state(STATE_PATH, STATUS_PATH)
    else:
        state_before = RuntimeState(
            version=1,
            started_at=datetime.now(tz=timezone.utc),
            status_file=str(STATUS_PATH),
            wallet=status_data.wallet,
            sol=status_data.sol,
            mints={},
        )
        for m in status_data.mints:
            balance_raw = int(getattr(m, "balance_raw", 0) or 0)
            entry = getattr(m, "entry", None)
            ep = entry.entry_price_sol_per_token if entry else 0.0
            src = getattr(entry, "entry_source", None) if entry else None
            trading_bag_raw, moonbag_raw = compute_trading_bag(
                str(balance_raw), config.trading_bag_pct
            )
            ensure_mint_state(
                state_before,
                m.mint,
                entry_price_sol_per_token=ep,
                trading_bag_raw=trading_bag_raw,
                moonbag_raw=moonbag_raw,
                entry_source=src if src and src != "unknown" else None,
            )

    before_records = compute_reconciliation_records(state_before, status_data)
    before_by_mint: Dict[str, Any] = {r.mint: r for r in before_records}

    # Build RPC client for full-history reads (no state mutation).
    delay_sec = max(
        0.0,
        min(int(os.environ.get("TX_BACKFILL_DELAY_MS", "200")) / 1000.0, 2.0),
    )
    primary = (os.environ.get("RPC_PRIMARY") or "").strip() or config.rpc_endpoint
    pool_list = [
        u.strip()
        for u in (os.environ.get("RPC_BACKFILL_POOL") or "").strip().split(",")
        if u.strip()
    ]
    rpc = BackfillRpcClient(
        primary_endpoint=primary,
        pool_endpoints=pool_list,
        timeout_s=getattr(config, "rpc_timeout_s", 20.0),
        delay_after_request_sec=delay_sec,
        max_retries_per_endpoint=2,
    )

    # Wallet-wide coverage: shared across mints.
    wallet_sigs, wallet_exhausted = _fetch_all_signatures(rpc, wallet_pubkey)
    wallet_earliest_ts: Optional[int] = None
    if wallet_sigs:
        by_bt = [s for s in wallet_sigs if s.get("blockTime") is not None]
        wallet_earliest_ts = (
            min(s["blockTime"] for s in by_bt) if by_bt else None
        )

    # Mints currently in the wallet: positive balance in status snapshot.
    active_status_mints = [
        m for m in status_data.mints if int(getattr(m, "balance_raw", 0) or 0) > 0
    ]

    per_mint_results: List[PerMintReconciliationSnapshot] = []

    for m in active_status_mints:
        mint = m.mint
        symbol = getattr(m, "symbol", None)
        balance_raw = int(getattr(m, "balance_raw", 0) or 0)

        before_rec = before_by_mint.get(mint)
        wallet_balance_raw = (
            int(before_rec.wallet_balance_raw) if before_rec else balance_raw
        )
        sum_before = (
            int(before_rec.sum_active_lots_raw)
            if before_rec is not None
            else 0
        )
        diff_before = getattr(before_rec, "diff_pct", None) if before_rec else None
        status_before_str = (
            getattr(before_rec, "reconciliation_status", None)
            if before_rec
            else None
        )
        blocker_before = (
            getattr(before_rec, "blocker_category", None) if before_rec else None
        )
        bag_zero_before = (
            getattr(before_rec, "bag_zero_reason", None) if before_rec else None
        )

        # Token-account coverage for this mint.
        token_account = _get_token_account_for_mint(rpc, wallet_pubkey, mint)
        ta_sigs: List[Dict[str, Any]] = []
        token_account_exhausted: Optional[bool] = None
        ta_earliest_ts: Optional[int] = None
        if token_account:
            ta_sigs, token_account_exhausted = _fetch_all_signatures(
                rpc, token_account
            )
            if ta_sigs:
                by_bt = [s for s in ta_sigs if s.get("blockTime") is not None]
                ta_earliest_ts = (
                    min(s["blockTime"] for s in by_bt) if by_bt else None
                )

        merged_sigs = _merge_and_sort_sigs(wallet_sigs, ta_sigs)

        # Build scratch state from status (all mints) – per-mint independence, but
        # preserve cross-mint cost basis semantics.
        scratch = RuntimeState(
            version=1,
            started_at=datetime.now(tz=timezone.utc),
            status_file=str(STATUS_PATH),
            wallet=status_data.wallet,
            sol=status_data.sol,
            mints={},
        )
        for sm in status_data.mints:
            bal_raw = int(getattr(sm, "balance_raw", 0) or 0)
            entry = getattr(sm, "entry", None)
            ep = entry.entry_price_sol_per_token if entry else 0.0
            src = getattr(entry, "entry_source", None) if entry else None
            trading_bag_raw, moonbag_raw = compute_trading_bag(
                str(bal_raw), config.trading_bag_pct
            )
            ensure_mint_state(
                scratch,
                sm.mint,
                entry_price_sol_per_token=ep,
                trading_bag_raw=trading_bag_raw,
                moonbag_raw=moonbag_raw,
                entry_source=src if src and src != "unknown" else None,
            )
        for ms in scratch.mints.values():
            _ensure_sell_accounting_backfill(ms)

        # Tx-first from merged history + external sells (scratch only).
        if merged_sigs:
            run_tx_first_lot_engine_from_sig_list(
                scratch,
                rpc,
                wallet_pubkey,
                merged_sigs,
                decimals_by_mint,
                journal_path=None,
                symbol_by_mint=symbol_by_mint,
                delay_after_request_sec=0.0,
            )
            _ingest_external_sells_from_sig_list(
                scratch,
                rpc,
                wallet_pubkey,
                merged_sigs,
                journal_path=None,
            )

        for ms in scratch.mints.values():
            if getattr(ms, "lots", None):
                ms.trading_bag_raw = str(_trading_bag_from_lots(ms))

        after_records = compute_reconciliation_records(
            scratch, status_data, mint_filter=mint
        )
        after_rec = after_records[0] if after_records else None

        sum_after = (
            int(after_rec.sum_active_lots_raw) if after_rec is not None else 0
        )
        diff_after = getattr(after_rec, "diff_pct", None) if after_rec else None
        status_after_str = (
            getattr(after_rec, "reconciliation_status", None)
            if after_rec
            else None
        )
        blocker_after = (
            getattr(after_rec, "blocker_category", None) if after_rec else None
        )
        bag_zero_after = (
            getattr(after_rec, "bag_zero_reason", None) if after_rec else None
        )

        ms_after = scratch.mints.get(mint)
        earliest_lot_dt: Optional[str] = None
        if ms_after is not None:
            detected_times = [
                l.detected_at
                for l in getattr(ms_after, "lots", None) or []
                if getattr(l, "detected_at", None) is not None
            ]
            if detected_times:
                earliest_lot_dt = min(detected_times).isoformat()

        dominant_blocker = blocker_after or blocker_before

        classification = _classify_resume_category(
            wallet_balance_raw=wallet_balance_raw,
            diff_pct_after=diff_after,
            reconciliation_status_after=status_after_str,
            blocker_category_after=blocker_after,
            provider_exhausted_wallet=wallet_exhausted,
            provider_exhausted_token_account=token_account_exhausted,
            bag_zero_reason_after=bag_zero_after,
        )

        pm = PerMintReconciliationSnapshot(
            symbol=symbol,
            mint=mint,
            wallet_balance_raw=wallet_balance_raw,
            sum_active_lots_raw_before=sum_before,
            sum_active_lots_raw_after=sum_after,
            diff_pct_before=diff_before,
            diff_pct_after=diff_after,
            reconciliation_status_before=status_before_str,
            reconciliation_status_after=status_after_str,
            blocker_category_before=blocker_before,
            blocker_category_after=blocker_after,
            earliest_wallet_tx_for_mint=_ts_display(wallet_earliest_ts),
            earliest_token_account_tx_for_mint=_ts_display(ta_earliest_ts),
            earliest_lot_after_rebuild=earliest_lot_dt,
            provider_exhausted_wallet=wallet_exhausted,
            provider_exhausted_token_account=token_account_exhausted,
            dominant_blocker_category=dominant_blocker,
            resume_classification=classification,
        )
        per_mint_results.append(pm)

    rpc.close()

    # Aggregate summary.
    summary_counts: Dict[str, int] = {}
    for r in per_mint_results:
        summary_counts[r.resume_classification] = (
            summary_counts.get(r.resume_classification, 0) + 1
        )

    # Rank top safe-resume candidates by quality + approximate size.
    def _status_rank(s: Optional[str]) -> int:
        s = (s or "").lower()
        if s == "sufficient":
            return 2
        if s == "partial":
            return 1
        return 0

    top_candidates: List[Dict[str, Any]] = []
    for r in per_mint_results:
        if not r.resume_classification.startswith("A_") and not r.resume_classification.startswith(
            "B_"
        ):
            continue
        dec = decimals_by_mint.get(r.mint, 6)
        human_amount = (
            r.wallet_balance_raw / float(10 ** dec) if dec >= 0 else 0.0
        )
        before_rec = before_by_mint.get(r.mint)
        entry_price = 0.0
        # Best-effort: use entry price from baseline mint state when available.
        if before_rec is not None:
            # ReconciliationRecord does not carry entry; ignore.
            entry_price = 0.0
        size_value_score = human_amount  # Without reliable price, size≈amount.
        quality = _status_rank(r.reconciliation_status_after)
        abs_diff = abs(r.diff_pct_after) if r.diff_pct_after is not None else None
        top_candidates.append(
            {
                "mint": r.mint,
                "symbol": r.symbol,
                "classification": r.resume_classification,
                "reconciliation_status_after": r.reconciliation_status_after,
                "diff_pct_after": r.diff_pct_after,
                "wallet_balance_raw": r.wallet_balance_raw,
                "wallet_amount": human_amount,
                "quality_rank": quality,
                "size_value_score": size_value_score,
                "abs_diff_after": abs_diff,
            }
        )

    top_candidates.sort(
        key=lambda x: (
            -(x["quality_rank"] or 0),
            (x["abs_diff_after"] if x["abs_diff_after"] is not None else 1.0),
            -(x["size_value_score"] or 0.0),
        )
    )
    top_candidates = top_candidates[:10]

    # Biggest unresolved blockers: non-safe categories, sorted by residual diff.
    unresolved = []
    for r in per_mint_results:
        if r.resume_classification.startswith("A_"):
            continue
        abs_diff = abs(r.diff_pct_after) if r.diff_pct_after is not None else None
        unresolved.append(
            {
                "mint": r.mint,
                "symbol": r.symbol,
                "classification": r.resume_classification,
                "reconciliation_status_after": r.reconciliation_status_after,
                "diff_pct_after": r.diff_pct_after,
                "abs_diff_after": abs_diff,
                "dominant_blocker_category": r.dominant_blocker_category,
            }
        )
    unresolved.sort(
        key=lambda x: (-(x["abs_diff_after"] or 0.0))
    )
    unresolved = unresolved[:10]

    json_report = {
        "wallet": wallet_pubkey,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_mints_analyzed": len(per_mint_results),
        "summary_counts_by_classification": summary_counts,
        "per_mint": [asdict(r) for r in per_mint_results],
        "top_safe_resume_candidates": top_candidates,
        "biggest_unresolved_blockers": unresolved,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_REPORT_PATH.write_text(json.dumps(json_report, indent=2), encoding="utf-8")

    # Markdown report (human-readable).
    lines: List[str] = []
    lines.append(f"# Full-wallet reconciliation report")
    lines.append("")
    lines.append(f"- Wallet: `{wallet_pubkey}`")
    lines.append(
        f"- Generated at: {json_report['generated_at']}"
    )
    lines.append(
        f"- Total mints analyzed: {json_report['total_mints_analyzed']}"
    )
    lines.append("")
    lines.append("## Summary by classification")
    for cls_key, count in sorted(summary_counts.items()):
        lines.append(f"- **{cls_key}**: {count}")
    lines.append("")

    if top_candidates:
        lines.append("## Top safe-resume candidates")
        for c in top_candidates:
            lines.append(
                f"- **{c['symbol'] or c['mint'][:8]}** ({c['mint']}): "
                f"class={c['classification']}, status={c['reconciliation_status_after']}, "
                f"diff_after={c['diff_pct_after']}, "
                f"amount={c['wallet_amount']}"
            )
        lines.append("")

    if unresolved:
        lines.append("## Biggest unresolved blockers")
        for u in unresolved:
            lines.append(
                f"- **{u['symbol'] or u['mint'][:8]}** ({u['mint']}): "
                f"class={u['classification']}, status={u['reconciliation_status_after']}, "
                f"diff_after={u['diff_pct_after']}, blocker={u['dominant_blocker_category']}"
            )
        lines.append("")

    MD_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"JSON report written to {JSON_REPORT_PATH}")
    print(f"Markdown report written to {MD_REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

