from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from .deep_history_scan import DeepHistoryScanResult, deep_scan_with_checkpoint
from .history_checkpoint import HistoryCheckpoint, HistoryPageEntry
from .models import RuntimeState, StatusFile
from .reconciliation_report import compute_reconciliation_records

FetchTxsFn = Callable[[Optional[str], int], Sequence[Dict]]


@dataclass
class MintHistoryAnalysisResult:
    mint: str
    symbol: Optional[str]
    reconciliation_status: Optional[str]
    diff_pct: Optional[float]
    blocker_category: Optional[str]
    pages_scanned: int
    signatures_scanned: int
    oldest_slot: Optional[int]
    exhausted: bool
    mint_relevant_signatures: int
    likely_helpfulness: str
    blocker_category_confidence: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "reconciliation_status": self.reconciliation_status,
            "diff_pct": self.diff_pct,
            "blocker_category": self.blocker_category,
            "pages_scanned": self.pages_scanned,
            "signatures_scanned": self.signatures_scanned,
            "oldest_slot": self.oldest_slot,
            "exhausted": self.exhausted,
            "mint_relevant_signatures": self.mint_relevant_signatures,
            "likely_helpfulness": self.likely_helpfulness,
            "blocker_category_confidence": self.blocker_category_confidence,
        }


def _tx_is_relevant_to_mint(tx: Dict, wallet: str, mint: str) -> bool:
    """
    Minimal Helius Enhanced Tx relevance check for a given mint+wallet.

    A tx counts as relevant if the mint appears in pre/post token balances
    owned by the wallet.
    """
    meta = tx.get("meta") or {}
    for key in ("preTokenBalances", "postTokenBalances"):
        for bal in meta.get(key) or []:
            if bal.get("owner") == wallet and bal.get("mint") == mint:
                return True
    return False


def analyze_mint_history(
    state: RuntimeState,
    status: StatusFile,
    wallet: str,
    mint: str,
    fetch_txs: FetchTxsFn,
    initial_checkpoint: Optional[HistoryCheckpoint] = None,
    max_pages: int = 5,
    page_limit: int = 50,
) -> MintHistoryAnalysisResult:
    """
    Read-only mint-focused deep-history analysis.

    Combines reconciliation_report snapshot with deep-history scan progress
    and a simple mint-relevance counter (based on token balance changes).
    Does not mutate runtime state or trading behavior.
    """
    recs = compute_reconciliation_records(state, status, mint_filter=mint)
    rec = recs[0] if recs else None

    reconciliation_status = rec.reconciliation_status if rec else None
    diff_pct = rec.diff_pct if rec else None
    blocker_category = rec.blocker_category if rec else None
    symbol = rec.symbol if rec else None

    mint_relevant = 0

    def _fetch_page(before: Optional[str], limit: int):
        nonlocal mint_relevant
        txs = fetch_txs(before, limit)
        entries: List[HistoryPageEntry] = []
        for t in txs:
            sig = t.get("signature")
            slot = t.get("slot")
            if not sig or slot is None:
                continue
            if _tx_is_relevant_to_mint(t, wallet, mint):
                mint_relevant += 1
            entries.append(HistoryPageEntry(signature=str(sig), slot=int(slot)))
        return entries

    scan_res: DeepHistoryScanResult = deep_scan_with_checkpoint(
        fetch_page=_fetch_page,
        initial_checkpoint=initial_checkpoint,
        max_pages=max_pages,
        page_limit=page_limit,
    )

    # Derive likely_helpfulness and blocker_category_confidence heuristically.
    abs_diff = abs(diff_pct) if diff_pct is not None else None

    if (
        reconciliation_status == "insufficient"
        and blocker_category == "missing historical tx coverage"
    ):
        if mint_relevant > 0 and not scan_res.exhausted:
            likely_help = "high"
            blocker_conf = "high"
        elif mint_relevant > 0 and scan_res.exhausted:
            likely_help = "medium"
            blocker_conf = "high"
        else:
            likely_help = "medium"
            blocker_conf = "medium"
    else:
        if mint_relevant > 0:
            likely_help = "medium"
            blocker_conf = "medium"
        else:
            likely_help = "low"
            blocker_conf = "low"

    # If there is effectively no residual, downgrade helpfulness.
    if abs_diff is not None and abs_diff <= 0.02:
        likely_help = "low"

    return MintHistoryAnalysisResult(
        mint=mint,
        symbol=symbol,
        reconciliation_status=reconciliation_status,
        diff_pct=diff_pct,
        blocker_category=blocker_category,
        pages_scanned=scan_res.pages_scanned,
        signatures_scanned=scan_res.signatures_scanned,
        oldest_slot=scan_res.oldest_slot,
        exhausted=scan_res.exhausted,
        mint_relevant_signatures=mint_relevant,
        likely_helpfulness=likely_help,
        blocker_category_confidence=blocker_conf,
    )

