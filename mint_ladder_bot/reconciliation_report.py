from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .bag_zero_reason import classify_bag_zero_reason
from .models import RuntimeMintState, RuntimeState, StatusFile


@dataclass
class ReconciliationRecord:
    mint: str
    symbol: Optional[str]
    wallet_balance_raw: int
    sum_active_lots_raw: int
    tx_derived_lots_count: int
    bootstrap_lots_count: int
    unknown_or_transfer_lots_count: int
    sold_bot_raw: int
    sold_external_raw: int
    explained_raw: int
    unexplained_raw: int
    diff_pct: Optional[float]
    blocker_category: str
    reconciliation_status: str
    bag_zero_reason: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "wallet_balance_raw": self.wallet_balance_raw,
            "sum_active_lots_raw": self.sum_active_lots_raw,
            "tx_derived_lots_count": self.tx_derived_lots_count,
            "bootstrap_lots_count": self.bootstrap_lots_count,
            "unknown_or_transfer_lots_count": self.unknown_or_transfer_lots_count,
            "sold_bot_raw": self.sold_bot_raw,
            "sold_external_raw": self.sold_external_raw,
            "explained_raw": self.explained_raw,
            "unexplained_raw": self.unexplained_raw,
            "diff_pct": self.diff_pct,
            "blocker_category": self.blocker_category,
            "reconciliation_status": self.reconciliation_status,
            "bag_zero_reason": self.bag_zero_reason,
        }


def _lot_source_counts(mint_state: RuntimeMintState) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for lot in getattr(mint_state, "lots", None) or []:
        src = getattr(lot, "source", "unknown") or "unknown"
        out[src] = out.get(src, 0) + 1
    return out


def _classify_blocker(
    wallet_balance_raw: int,
    sum_active_lots_raw: int,
    tx_derived_lots_count: int,
    bootstrap_lots_count: int,
    unknown_or_transfer_lots_count: int,
) -> str:
    # Bootstrap-only inventory: no tx-derived lots, but snapshot/unknown lots exist.
    if tx_derived_lots_count == 0 and (bootstrap_lots_count > 0 or unknown_or_transfer_lots_count > 0):
        return "bootstrap-only inventory"
    # Unknown provenance: transfer-like or other non-tx sources dominate.
    if unknown_or_transfer_lots_count > 0 and tx_derived_lots_count == 0:
        return "wallet transfer / unknown provenance"
    # Large residual with tx-derived lots present: likely incomplete history.
    if wallet_balance_raw > 0:
        diff_raw = wallet_balance_raw - sum_active_lots_raw
        diff_pct = abs(diff_raw) / wallet_balance_raw
        if diff_pct > 0.25:
            return "missing historical tx coverage"
    return "other"


def _classify_reconciliation_status(wallet_balance_raw: int, explained_raw: int) -> str:
    """
    Classify reconciliation status based on absolute unexplained fraction.

    Note: this uses the absolute percentage difference between wallet_balance_raw
    and explained_raw. Reporting (diff_pct in ReconciliationRecord) keeps the
    signed ratio (unexplained / wallet) so callers can see the direction
    (lots > wallet vs wallet > lots), but classification itself is driven by
    the absolute value:

    - <= 2%  -> 'sufficient'
    - 2–25%  -> 'partial'
    - > 25%  -> 'insufficient'
    """
    if wallet_balance_raw <= 0:
        return "sufficient"
    diff_raw = wallet_balance_raw - explained_raw
    diff_pct = abs(diff_raw) / wallet_balance_raw
    if diff_pct <= 0.02:
        return "sufficient"
    if diff_pct <= 0.25:
        return "partial"
    return "insufficient"


def compute_reconciliation_records(
    state: RuntimeState,
    status: StatusFile,
    mint_filter: Optional[str] = None,
) -> List[ReconciliationRecord]:
    """
    Compute reconciliation records for all mints (or a single mint when mint_filter is set).

    Read-only: does not mutate state or status. Suitable for diagnostics and tests.
    """
    status_by_mint: Dict[str, object] = {m.mint: m for m in status.mints}
    records: List[ReconciliationRecord] = []

    for mint_addr, ms in state.mints.items():
        if mint_filter and mint_addr != mint_filter:
            continue

        st = status_by_mint.get(mint_addr)
        wallet_raw = 0
        if st is not None:
            try:
                wallet_raw = int(getattr(st, "balance_raw", "0") or 0)
            except (ValueError, TypeError):
                wallet_raw = 0

        lots = getattr(ms, "lots", None) or []
        active_lots = [l for l in lots if getattr(l, "status", "active") == "active"]
        sum_active = 0
        for l in active_lots:
            try:
                sum_active += int(getattr(l, "remaining_amount", "0") or 0)
            except (ValueError, TypeError):
                continue

        src_counts = _lot_source_counts(ms)
        tx_derived_lots = sum(
            c for s, c in src_counts.items() if s in ("tx_exact", "tx_parsed")
        )
        bootstrap_lots = src_counts.get("bootstrap_snapshot", 0)
        unknown_or_transfer_lots = sum(
            c for s, c in src_counts.items() if s not in ("tx_exact", "tx_parsed", "bootstrap_snapshot")
        )

        sold_bot = 0
        if getattr(ms, "sold_bot_raw", None) is not None:
            try:
                sold_bot = int(ms.sold_bot_raw or 0)
            except (ValueError, TypeError):
                sold_bot = 0

        sold_ext = 0
        if getattr(ms, "sold_external_raw", None) is not None:
            try:
                sold_ext = int(ms.sold_external_raw or 0)
            except (ValueError, TypeError):
                sold_ext = 0

        explained = sum_active
        unexplained = wallet_raw - explained
        diff_pct = None
        if wallet_raw > 0:
            diff_pct = unexplained / wallet_raw

        bag_zero = classify_bag_zero_reason(ms.model_dump(), wallet_balance_raw=wallet_raw)
        blocker = _classify_blocker(
            wallet_raw,
            sum_active,
            tx_derived_lots,
            bootstrap_lots,
            unknown_or_transfer_lots,
        )
        reconciliation_status = _classify_reconciliation_status(wallet_raw, explained)

        records.append(
            ReconciliationRecord(
                mint=mint_addr,
                symbol=getattr(st, "symbol", None) if st is not None else None,
                wallet_balance_raw=wallet_raw,
                sum_active_lots_raw=sum_active,
                tx_derived_lots_count=tx_derived_lots,
                bootstrap_lots_count=bootstrap_lots,
                unknown_or_transfer_lots_count=unknown_or_transfer_lots,
                sold_bot_raw=sold_bot,
                sold_external_raw=sold_ext,
                explained_raw=explained,
                unexplained_raw=unexplained,
                diff_pct=diff_pct,
                blocker_category=blocker,
                reconciliation_status=reconciliation_status,
                bag_zero_reason=bag_zero,
            )
        )

    return records

