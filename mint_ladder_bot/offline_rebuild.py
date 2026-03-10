from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .models import RuntimeMintState, RuntimeState, StatusFile
from .reconciliation_report import ReconciliationRecord, compute_reconciliation_records
from .tx_lot_engine import run_tx_first_lot_engine
from .runner import _ingest_external_sells, _trading_bag_from_lots


@dataclass
class MintRebuildComparison:
    mint: str
    symbol: Optional[str]
    lot_count_before: int
    lot_count_after: int
    sum_active_lots_raw_before: int
    sum_active_lots_raw_after: int
    diff_pct_before: Optional[float]
    diff_pct_after: Optional[float]
    blocker_category_before: Optional[str]
    blocker_category_after: Optional[str]
    reconciliation_status_before: Optional[str]
    reconciliation_status_after: Optional[str]
    improved: bool

    def to_dict(self) -> Dict[str, object]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "lot_count_before": self.lot_count_before,
            "lot_count_after": self.lot_count_after,
            "sum_active_lots_raw_before": self.sum_active_lots_raw_before,
            "sum_active_lots_raw_after": self.sum_active_lots_raw_after,
            "diff_pct_before": self.diff_pct_before,
            "diff_pct_after": self.diff_pct_after,
            "blocker_category_before": self.blocker_category_before,
            "blocker_category_after": self.blocker_category_after,
            "reconciliation_status_before": self.reconciliation_status_before,
            "reconciliation_status_after": self.reconciliation_status_after,
            "improved": self.improved,
        }


def _get_record_for_mint(
    records: List[ReconciliationRecord],
    mint: str,
) -> Optional[ReconciliationRecord]:
    for r in records:
        if r.mint == mint:
            return r
    return None


def run_scratch_rebuild_for_mint(
    status: StatusFile,
    state_before: RuntimeState,
    wallet_pubkey: str,
    mint: str,
    rpc,
    max_signatures: int,
    decimals_by_mint: Dict[str, int],
    symbol_by_mint: Dict[str, str],
) -> RuntimeState:
    """
    Build a scratch RuntimeState for a single mint using tx-first engine + external sells.

    - Starts from empty lots for the target mint.
    - Uses tx-first lot engine to create tx-derived lots from history.
    - Uses external-sell ingestion to debit lots.
    - Does NOT mutate state_before or any on-disk state.
    """
    # Minimal scratch state: preserve wallet/sol/version but clear mints.
    scratch = RuntimeState(
        version=getattr(state_before, "version", 1),
        started_at=state_before.started_at,
        status_file=state_before.status_file,
        wallet=state_before.wallet,
        sol=state_before.sol,
        mints={},
    )

    # Ensure mint exists in scratch state with empty lots; copy structural fields from before if present.
    ms_before: Optional[RuntimeMintState] = state_before.mints.get(mint)
    if ms_before is not None:
        ms_scratch = ms_before.model_copy(deep=True)
        ms_scratch.lots = []
        ms_scratch.trading_bag_raw = "0"
        ms_scratch.sold_bot_raw = "0"
        ms_scratch.sold_external_raw = "0"
        ms_scratch.executed_steps = {}
    else:
        # Fallback: create a minimal mint state with no lots and placeholder entry.
        ms_scratch = RuntimeMintState(
            entry_price_sol_per_token=0.0,
            trading_bag_raw="0",
            moonbag_raw="0",
            lots=[],
        )
    scratch.mints[mint] = ms_scratch

    # Run tx-first lot engine for this mint only.
    run_tx_first_lot_engine(
        state=scratch,
        rpc=rpc,
        wallet_pubkey=wallet_pubkey,
        decimals_by_mint=decimals_by_mint,
        journal_path=None,
        max_signatures=max_signatures,
        symbol_by_mint=symbol_by_mint,
        delay_after_request_sec=0.0,
    )

    # Ingest external sells and recompute trading bag.
    _ingest_external_sells(
        state=scratch,
        rpc=rpc,
        wallet=wallet_pubkey,
        max_signatures=max_signatures,
        journal_path=None,
        config=config,
    )
    for ms in scratch.mints.values():
        if getattr(ms, "lots", None):
            ms.trading_bag_raw = str(_trading_bag_from_lots(ms))

    return scratch


def run_deep_rebuild_comparison(
    status: StatusFile,
    state_before: RuntimeState,
    wallet_pubkey: str,
    mint: str,
    rpc,
    max_signatures: int,
    decimals_by_mint: Dict[str, int],
    symbol_by_mint: Dict[str, str],
) -> MintRebuildComparison:
    """
    Compare reconciliation metrics before vs after scratch deep-history reconstruction for one mint.

    Read-only w.r.t. live state: does not mutate state_before or any runtime files.
    """
    # Baseline metrics from current state.
    before_records = compute_reconciliation_records(state_before, status, mint_filter=mint)
    before_rec = before_records[0] if before_records else None

    ms_before = state_before.mints.get(mint)
    lot_count_before = len(getattr(ms_before, "lots", None) or []) if ms_before is not None else 0
    sum_active_before = before_rec.sum_active_lots_raw if before_rec is not None else 0
    diff_pct_before = before_rec.diff_pct if before_rec is not None else None
    blocker_before = before_rec.blocker_category if before_rec is not None else None
    status_before_str = before_rec.reconciliation_status if before_rec is not None else None

    # Scratch reconstruction.
    scratch = run_scratch_rebuild_for_mint(
        status=status,
        state_before=state_before,
        wallet_pubkey=wallet_pubkey,
        mint=mint,
        rpc=rpc,
        max_signatures=max_signatures,
        decimals_by_mint=decimals_by_mint,
        symbol_by_mint=symbol_by_mint,
    )

    after_records = compute_reconciliation_records(scratch, status, mint_filter=mint)
    after_rec = after_records[0] if after_records else None
    ms_after = scratch.mints.get(mint)
    lot_count_after = len(getattr(ms_after, "lots", None) or []) if ms_after is not None else 0
    sum_active_after = after_rec.sum_active_lots_raw if after_rec is not None else 0
    diff_pct_after = after_rec.diff_pct if after_rec is not None else None
    blocker_after = after_rec.blocker_category if after_rec is not None else None
    status_after_str = after_rec.reconciliation_status if after_rec is not None else None

    # Improvement heuristic: stricter rule for "material" improvement.
    # - Prefer better reconciliation_status (insufficient -> partial -> sufficient)
    # - If status unchanged, require smaller absolute residual AND non-trivial lots.
    # - Zero-lot scratch outcomes are never treated as meaningful improvement.
    status_rank = {"insufficient": 0, "partial": 1, "sufficient": 2}
    improved = False
    nontrivial_after = lot_count_after > 0 and sum_active_after > 0
    if status_before_str and status_after_str and nontrivial_after:
        if status_rank.get(status_after_str, 0) > status_rank.get(status_before_str, 0):
            improved = True
        elif status_before_str == status_after_str and diff_pct_before is not None and diff_pct_after is not None:
            if abs(diff_pct_after) + 1e-9 < abs(diff_pct_before):
                improved = True

    symbol = None
    for m in status.mints:
        if m.mint == mint:
            symbol = getattr(m, "symbol", None)
            break

    return MintRebuildComparison(
        mint=mint,
        symbol=symbol,
        lot_count_before=lot_count_before,
        lot_count_after=lot_count_after,
        sum_active_lots_raw_before=sum_active_before,
        sum_active_lots_raw_after=sum_active_after,
        diff_pct_before=diff_pct_before,
        diff_pct_after=diff_pct_after,
        blocker_category_before=blocker_before,
        blocker_category_after=blocker_after,
        reconciliation_status_before=status_before_str,
        reconciliation_status_after=status_after_str,
        improved=improved,
    )

