"""
Step 3: Scratch reconstruction for one trusted source wallet only.

Reconstructs source wallet history in scratch, attributes transferred amount
to provenance-valid lots (FIFO), and reports before/after reconciliation.
Does NOT mutate live state, create persistent lots, or resume mints.

Design: docs/trading/launch-time-reconstruction-transfer-provenance-design.md
"""
from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import LotInfo, RuntimeMintState, RuntimeState, StatusFile
from .reconciliation_report import (
    ReconciliationRecord,
    compute_reconciliation_records,
)
from .tx_lot_engine import run_tx_first_lot_engine

logger = logging.getLogger(__name__)

# Proposed classification (scratch-only)
PROPOSED_TRUSTED_TRANSFER_DERIVED = "proposed_trusted_transfer_derived"
REMAINS_UNTRUSTED_OR_NONTRADABLE = "remains_untrusted_or_nontradable"

# Source reconstruction status
SRC_RECON_SUCCESS = "success"
SRC_RECON_INSUFFICIENT = "insufficient"
SRC_RECON_FAILED = "failed"


def _provenance_valid_lots_ordered(
    mint_state: RuntimeMintState,
) -> List[Tuple[Any, int]]:
    """
    Return (lot, remaining_amount_raw) for tx_exact/tx_parsed lots only,
    ordered by detected_at ascending (FIFO). Excludes non-active.
    """
    lots = getattr(mint_state, "lots", None) or []
    out: List[Tuple[Any, int]] = []
    for lot in lots:
        if getattr(lot, "status", "active") != "active":
            continue
        src = getattr(lot, "source", "") or ""
        if src not in ("tx_exact", "tx_parsed"):
            continue
        try:
            rem = int(getattr(lot, "remaining_amount", 0) or 0)
        except (ValueError, TypeError):
            rem = 0
        if rem <= 0:
            continue
        out.append((lot, rem))
    # Sort by detected_at if present (FIFO = oldest first)
    def _key(item: Tuple[Any, int]) -> datetime:
        lot = item[0]
        t = getattr(lot, "detected_at", None)
        if t is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        return t if getattr(t, "tzinfo", None) else t.replace(tzinfo=timezone.utc)

    out.sort(key=_key)
    return out


def _fifo_attribution(
    lots_with_remaining: List[Tuple[Any, int]],
    amount_raw: int,
    decimals: int,
) -> Tuple[int, float, bool]:
    """
    FIFO attribution: take from lots until we cover amount_raw.
    Returns (attributed_amount_raw, cost_sol, success).
    cost_sol = sum(take * entry_price_sol_per_token) in human units.
    success = attributed_amount_raw == amount_raw and all entry prices valid.
    """
    if amount_raw <= 0:
        return 0, 0.0, True
    remaining_to_cover = amount_raw
    cost_sol = 0.0
    for lot, rem in lots_with_remaining:
        if remaining_to_cover <= 0:
            break
        ep = getattr(lot, "entry_price_sol_per_token", None)
        if ep is None or ep <= 0 or ep < 1e-12 or ep > 1e3:
            return 0, 0.0, False
        take = min(rem, remaining_to_cover)
        token_human = take / (10 ** decimals)
        cost_sol += token_human * ep
        remaining_to_cover -= take
    if remaining_to_cover > 0:
        return amount_raw - remaining_to_cover, cost_sol, False
    return amount_raw, cost_sol, True


def run_source_wallet_scratch_reconstruction(
    source_wallet: str,
    mint: str,
    rpc: Any,
    max_signatures: int,
    decimals_by_mint: Dict[str, int],
    symbol_by_mint: Optional[Dict[str, str]] = None,
) -> Tuple[RuntimeState, str]:
    """
    Build scratch RuntimeState for the source wallet and one mint using tx-first.
    Returns (scratch_state, status) where status is SRC_RECON_SUCCESS, SRC_RECON_INSUFFICIENT, or SRC_RECON_FAILED.
    Does NOT mutate any on-disk state.
    """
    decimals_by_mint = decimals_by_mint or {}
    symbol_by_mint = symbol_by_mint or {}
    scratch = RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file="",
        wallet=source_wallet,
        sol=None,
        mints={},
    )
    ms = RuntimeMintState(
        entry_price_sol_per_token=0.0,
        trading_bag_raw="0",
        moonbag_raw="0",
        lots=[],
    )
    scratch.mints[mint] = ms

    try:
        n_created = run_tx_first_lot_engine(
            state=scratch,
            rpc=rpc,
            wallet_pubkey=source_wallet,
            decimals_by_mint=decimals_by_mint,
            journal_path=None,
            max_signatures=max_signatures,
            symbol_by_mint=symbol_by_mint,
            delay_after_request_sec=0.0,
        )
    except Exception as exc:
        logger.warning("Source wallet scratch reconstruction failed: %s", exc)
        return scratch, SRC_RECON_FAILED

    lots_ordered = _provenance_valid_lots_ordered(ms)
    total_valid = sum(rem for _, rem in lots_ordered)
    if total_valid == 0 and len(getattr(ms, "lots", None) or []) > 0:
        return scratch, SRC_RECON_INSUFFICIENT
    if total_valid == 0:
        return scratch, SRC_RECON_INSUFFICIENT
    return scratch, SRC_RECON_SUCCESS


@dataclass
class ScratchCandidateReport:
    """Per-candidate scratch analysis report (read-only)."""

    destination_wallet: str
    source_wallet: str
    mint: str
    symbol: Optional[str]
    tx_signature: str
    transferred_amount_raw: int
    source_reconstruction_status: str
    source_provenance_valid_available_raw: int
    amount_propagated_raw: int
    proposed_classification: str
    proposed_entry_price_sol_per_token: Optional[float]
    why: str
    before: Dict[str, Any]
    after: Dict[str, Any]
    improvement_meaningful: bool
    improvement_numerical_only: bool
    satisfies_strict_safety_rule: bool
    proposed_lot_in_memory: Optional[LotInfo] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "destination_wallet": self.destination_wallet,
            "source_wallet": self.source_wallet,
            "mint": self.mint,
            "symbol": self.symbol,
            "tx_signature": self.tx_signature,
            "transferred_amount_raw": self.transferred_amount_raw,
            "source_reconstruction_status": self.source_reconstruction_status,
            "source_provenance_valid_available_raw": self.source_provenance_valid_available_raw,
            "amount_propagated_raw": self.amount_propagated_raw,
            "proposed_classification": self.proposed_classification,
            "proposed_entry_price_sol_per_token": self.proposed_entry_price_sol_per_token,
            "why": self.why,
            "before": self.before,
            "after": self.after,
            "improvement_meaningful": self.improvement_meaningful,
            "improvement_numerical_only": self.improvement_numerical_only,
            "satisfies_strict_safety_rule": self.satisfies_strict_safety_rule,
        }


def run_scratch_trusted_transfer_analysis(
    destination_wallet: str,
    destination_state: RuntimeState,
    status: StatusFile,
    mint: str,
    source_wallet: str,
    tx_signature: str,
    transferred_amount_raw: int,
    trusted_source_wallets: List[str],
    rpc: Any,
    max_signatures: int,
    decimals_by_mint: Dict[str, int],
    symbol_by_mint: Optional[Dict[str, str]] = None,
) -> ScratchCandidateReport:
    """
    Run scratch reconstruction for one trusted transfer candidate.
    Does NOT mutate destination_state or any on-disk state.
    """
    symbol = None
    for m in status.mints:
        if m.mint == mint:
            symbol = getattr(m, "symbol", None)
            break
    decimals = decimals_by_mint.get(mint, 6)

    # 1) Reject if source not trusted
    if source_wallet not in (trusted_source_wallets or []):
        before_records = compute_reconciliation_records(
            destination_state, status, mint_filter=mint
        )
        before_rec = before_records[0] if before_records else None
        _lot_count = len(getattr(destination_state.mints.get(mint), "lots", None) or [])
        before_dict = _record_to_metrics(before_rec, lot_count=_lot_count)
        return ScratchCandidateReport(
            destination_wallet=destination_wallet,
            source_wallet=source_wallet,
            mint=mint,
            symbol=symbol,
            tx_signature=tx_signature,
            transferred_amount_raw=transferred_amount_raw,
            source_reconstruction_status=SRC_RECON_FAILED,
            source_provenance_valid_available_raw=0,
            amount_propagated_raw=0,
            proposed_classification=REMAINS_UNTRUSTED_OR_NONTRADABLE,
            proposed_entry_price_sol_per_token=None,
            why="source_wallet_not_in_trusted_source_wallets",
            before=before_dict,
            after=before_dict,
            improvement_meaningful=False,
            improvement_numerical_only=False,
            satisfies_strict_safety_rule=False,
        )

    # 2) Before metrics (destination mint)
    before_records = compute_reconciliation_records(
        destination_state, status, mint_filter=mint
    )
    before_rec = before_records[0] if before_records else None
    before_lot_count = len(getattr(destination_state.mints.get(mint), "lots", None) or [])
    before_dict = _record_to_metrics(before_rec, lot_count=before_lot_count)

    # 3) Reconstruct source wallet in scratch
    source_scratch, recon_status = run_source_wallet_scratch_reconstruction(
        source_wallet=source_wallet,
        mint=mint,
        rpc=rpc,
        max_signatures=max_signatures,
        decimals_by_mint=decimals_by_mint,
        symbol_by_mint=symbol_by_mint,
    )
    source_ms = source_scratch.mints.get(mint)
    lots_ordered = _provenance_valid_lots_ordered(source_ms) if source_ms else []
    provenance_valid_available = sum(rem for _, rem in lots_ordered)

    if recon_status != SRC_RECON_SUCCESS:
        return ScratchCandidateReport(
            destination_wallet=destination_wallet,
            source_wallet=source_wallet,
            mint=mint,
            symbol=symbol,
            tx_signature=tx_signature,
            transferred_amount_raw=transferred_amount_raw,
            source_reconstruction_status=recon_status,
            source_provenance_valid_available_raw=provenance_valid_available,
            amount_propagated_raw=0,
            proposed_classification=REMAINS_UNTRUSTED_OR_NONTRADABLE,
            proposed_entry_price_sol_per_token=None,
            why=f"source_reconstruction_{recon_status}",
            before=before_dict,
            after=before_dict,
            improvement_meaningful=False,
            improvement_numerical_only=False,
            satisfies_strict_safety_rule=False,
        )

    # 4) FIFO attribution
    attributed_raw, cost_sol, attr_ok = _fifo_attribution(
        lots_ordered, transferred_amount_raw, decimals
    )
    if not attr_ok or attributed_raw < transferred_amount_raw:
        return ScratchCandidateReport(
            destination_wallet=destination_wallet,
            source_wallet=source_wallet,
            mint=mint,
            symbol=symbol,
            tx_signature=tx_signature,
            transferred_amount_raw=transferred_amount_raw,
            source_reconstruction_status=SRC_RECON_SUCCESS,
            source_provenance_valid_available_raw=provenance_valid_available,
            amount_propagated_raw=attributed_raw,
            proposed_classification=REMAINS_UNTRUSTED_OR_NONTRADABLE,
            proposed_entry_price_sol_per_token=None,
            why="fifo_attribution_insufficient_or_invalid_entry",
            before=before_dict,
            after=before_dict,
            improvement_meaningful=False,
            improvement_numerical_only=False,
            satisfies_strict_safety_rule=False,
        )

    # 5) Cap propagated amount by valid source inventory (already satisfied by FIFO)
    amount_propagated = attributed_raw
    if amount_propagated > provenance_valid_available:
        amount_propagated = provenance_valid_available
    entry_sol = cost_sol / (amount_propagated / (10 ** decimals)) if amount_propagated else None
    if entry_sol is not None and (entry_sol < 1e-12 or entry_sol > 1e3):
        entry_sol = None

    # 6) Proposed lot (in memory only)
    proposed_lot = LotInfo.create(
        mint=mint,
        token_amount_raw=amount_propagated,
        entry_price=entry_sol,
        confidence="inferred",
        source="trusted_transfer_derived",
        entry_confidence="inferred",
        tx_signature=tx_signature,
    )
    if hasattr(proposed_lot, "source_type"):
        proposed_lot.source_type = "trusted_transfer_derived"

    # 7) After state = destination copy + proposed lot (scratch only)
    after_state = deepcopy(destination_state)
    dest_ms = after_state.mints.get(mint)
    if dest_ms is None:
        dest_ms = RuntimeMintState(
            entry_price_sol_per_token=0.0,
            trading_bag_raw="0",
            moonbag_raw="0",
            lots=[],
        )
        after_state.mints[mint] = dest_ms
    dest_lots = list(getattr(dest_ms, "lots", None) or [])
    dest_lots.append(proposed_lot)
    dest_ms.lots = dest_lots
    # Recompute sum for display
    from .runner import _trading_bag_from_lots
    dest_ms.trading_bag_raw = str(_trading_bag_from_lots(dest_ms))

    after_records = compute_reconciliation_records(
        after_state, status, mint_filter=mint
    )
    after_rec = after_records[0] if after_records else None
    after_lot_count = len(getattr(after_state.mints.get(mint), "lots", None) or [])
    after_dict = _record_to_metrics(after_rec, lot_count=after_lot_count)

    # 8) Meaningful improvement?
    before_sum = before_rec.sum_active_lots_raw if before_rec else 0
    after_sum = after_rec.sum_active_lots_raw if after_rec else 0
    wallet_balance = before_rec.wallet_balance_raw if before_rec else 0
    improvement_numerical = after_sum > before_sum and (
        after_rec.diff_pct is not None
        and before_rec.diff_pct is not None
        and abs(after_rec.diff_pct) < abs(before_rec.diff_pct)
    )
    improvement_meaningful = (
        amount_propagated > 0
        and entry_sol is not None
        and improvement_numerical
    )
    satisfies = (
        source_wallet in (trusted_source_wallets or [])
        and recon_status == SRC_RECON_SUCCESS
        and attr_ok
        and amount_propagated <= provenance_valid_available
        and entry_sol is not None
    )
    proposed_classification = PROPOSED_TRUSTED_TRANSFER_DERIVED

    return ScratchCandidateReport(
        destination_wallet=destination_wallet,
        source_wallet=source_wallet,
        mint=mint,
        symbol=symbol,
        tx_signature=tx_signature,
        transferred_amount_raw=transferred_amount_raw,
        source_reconstruction_status=recon_status,
        source_provenance_valid_available_raw=provenance_valid_available,
        amount_propagated_raw=amount_propagated,
        proposed_classification=proposed_classification,
        proposed_entry_price_sol_per_token=entry_sol,
        why="accepted_provenance_valid_fifo_attribution",
        before=before_dict,
        after=after_dict,
        improvement_meaningful=improvement_meaningful,
        improvement_numerical_only=improvement_numerical and not improvement_meaningful,
        satisfies_strict_safety_rule=satisfies,
        proposed_lot_in_memory=proposed_lot,
    )


def _record_to_metrics(
    rec: Optional[ReconciliationRecord],
    lot_count: Optional[int] = None,
) -> Dict[str, Any]:
    if rec is None:
        return {
            "lot_count": lot_count if lot_count is not None else 0,
            "sum_active_lots_raw": 0,
            "wallet_balance_raw": 0,
            "diff_pct": None,
            "reconciliation_status": None,
            "blocker_category": None,
        }
    return {
        "lot_count": lot_count if lot_count is not None else 0,
        "sum_active_lots_raw": rec.sum_active_lots_raw,
        "wallet_balance_raw": getattr(rec, "wallet_balance_raw", 0),
        "diff_pct": rec.diff_pct,
        "reconciliation_status": rec.reconciliation_status,
        "blocker_category": rec.blocker_category,
    }


def _meaningful_improvement_rule(report: ScratchCandidateReport) -> Tuple[bool, str]:
    """
    Step 4: A candidate counts as meaningfully improved only if ALL are true:
    1. proposed_classification == proposed_trusted_transfer_derived
    2. amount_propagated_raw > 0
    3. proposed entry price is provenance-derived (not fabricated)
    4. reconciliation_status improves OR abs(diff_pct_after) materially decreases
    5. no new unknown residual introduced (we only add trusted_transfer_derived)
    6. after-state is safety-valid (sum_lots not exceeding balance materially)
    Returns (is_meaningful, explanation).
    """
    if report.proposed_classification != PROPOSED_TRUSTED_TRANSFER_DERIVED:
        return False, "proposed_classification is not proposed_trusted_transfer_derived"
    if report.amount_propagated_raw <= 0:
        return False, "amount_propagated_raw is zero; zero never counts as meaningful improvement"
    if report.proposed_entry_price_sol_per_token is None:
        return False, "proposed entry price is not provenance-derived"
    before = report.before
    after = report.after
    status_before = before.get("reconciliation_status")
    status_after = after.get("reconciliation_status")
    diff_before = before.get("diff_pct")
    diff_after = after.get("diff_pct")
    status_rank = {"insufficient": 0, "partial": 1, "sufficient": 2}
    rank_before = status_rank.get(status_before, -1)
    rank_after = status_rank.get(status_after, -1)
    status_improved = rank_after > rank_before
    diff_improved = False
    if diff_before is not None and diff_after is not None:
        if abs(diff_after) + 1e-9 < abs(diff_before):
            diff_improved = True
    if not status_improved and not diff_improved:
        return False, "reconciliation_status did not improve and abs(diff_pct) did not materially decrease"
    sum_after = after.get("sum_active_lots_raw", 0)
    if isinstance(sum_after, str):
        sum_after = int(sum_after)
    wallet_balance = report.before.get("wallet_balance_raw", 0)
    if isinstance(wallet_balance, str):
        wallet_balance = int(wallet_balance)
    if sum_after > wallet_balance * 1.02:
        return False, "after-state not safety-valid: sum_active_lots exceeds wallet_balance"
    return True, "all six conditions met: proposed trusted-transfer-derived, non-zero propagated, provenance-derived entry, reconciliation improved or diff decreased, no unknown residual, after-state valid"


def build_reconciliation_comparison_report(
    report: ScratchCandidateReport,
) -> Dict[str, Any]:
    """
    Build Step 4 structured comparison output from a ScratchCandidateReport.
    Applies the 6-point meaningful improvement rule and adds explanation.
    Read-only; does not mutate report.
    """
    improvement_meaningful, explanation = _meaningful_improvement_rule(report)
    improvement_numerical = (
        report.after.get("sum_active_lots_raw", 0) > report.before.get("sum_active_lots_raw", 0)
        and report.before.get("diff_pct") is not None
        and report.after.get("diff_pct") is not None
        and abs(report.after["diff_pct"]) < abs(report.before["diff_pct"])
    )
    return {
        "identity": {
            "destination_wallet": report.destination_wallet,
            "source_wallet": report.source_wallet,
            "mint": report.mint,
            "symbol": report.symbol,
            "tx_signature": report.tx_signature,
            "transferred_amount_raw": report.transferred_amount_raw,
        },
        "before": {
            "lot_count_before": report.before.get("lot_count", 0),
            "sum_active_lots_raw_before": report.before.get("sum_active_lots_raw", 0),
            "diff_pct_before": report.before.get("diff_pct"),
            "reconciliation_status_before": report.before.get("reconciliation_status"),
            "blocker_category_before": report.before.get("blocker_category"),
        },
        "after": {
            "lot_count_after": report.after.get("lot_count", 0),
            "sum_active_lots_raw_after": report.after.get("sum_active_lots_raw", 0),
            "diff_pct_after": report.after.get("diff_pct"),
            "reconciliation_status_after": report.after.get("reconciliation_status"),
            "blocker_category_after": report.after.get("blocker_category"),
        },
        "transfer_result": {
            "proposed_classification": report.proposed_classification,
            "amount_propagated_raw": report.amount_propagated_raw,
            "proposed_entry_price_sol_per_token": report.proposed_entry_price_sol_per_token,
            "source_reconstruction_status": report.source_reconstruction_status,
            "why_accepted_or_rejected": report.why,
        },
        "improvement_assessment": {
            "improvement_numerical": improvement_numerical,
            "improvement_meaningful": improvement_meaningful,
            "explanation": explanation,
        },
    }


def run_scratch_report_for_candidates(
    destination_wallet: str,
    destination_state: RuntimeState,
    status: StatusFile,
    candidates: List[Any],
    trusted_source_wallets: List[str],
    rpc: Any,
    max_signatures: int,
    decimals_by_mint: Dict[str, int],
    symbol_by_mint: Optional[Dict[str, str]] = None,
) -> List[ScratchCandidateReport]:
    """
    Run scratch analysis for each candidate that is a trusted-transfer-candidate.
    Candidates must have: mint, source_wallet, tx_signature, amount_raw, classification.
    Returns list of ScratchCandidateReport (one per trusted candidate analyzed).
    """
    from .transfer_provenance_analysis import CLASS_TRUSTED_TRANSFER_CANDIDATE

    reports: List[ScratchCandidateReport] = []
    for c in candidates:
        if getattr(c, "classification", None) != CLASS_TRUSTED_TRANSFER_CANDIDATE:
            continue
        mint = getattr(c, "mint", None)
        src = getattr(c, "source_wallet", None)
        sig = getattr(c, "tx_signature", None)
        amt = getattr(c, "amount_raw", 0)
        if not mint or not src or not sig or amt <= 0:
            continue
        try:
            amt_int = int(amt)
        except (ValueError, TypeError):
            continue
        report = run_scratch_trusted_transfer_analysis(
            destination_wallet=destination_wallet,
            destination_state=destination_state,
            status=status,
            mint=mint,
            source_wallet=src,
            tx_signature=sig,
            transferred_amount_raw=amt_int,
            trusted_source_wallets=trusted_source_wallets,
            rpc=rpc,
            max_signatures=max_signatures,
            decimals_by_mint=decimals_by_mint,
            symbol_by_mint=symbol_by_mint,
        )
        reports.append(report)
    return reports
