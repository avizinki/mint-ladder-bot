from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from .history_checkpoint import (
    HistoryCheckpoint,
    HistoryPageEntry,
    advance_checkpoint,
    init_checkpoint_from_page,
    next_before_anchor,
)


FetchPageFn = Callable[[Optional[str], int], Sequence[HistoryPageEntry]]


@dataclass
class DeepHistoryScanResult:
    checkpoint: HistoryCheckpoint
    pages_scanned: int
    signatures_scanned: int
    oldest_slot: Optional[int]
    exhausted: bool

    def to_dict(self) -> dict:
        return {
            "checkpoint": {
                "earliest_signature": self.checkpoint.earliest_signature,
                "earliest_slot": self.checkpoint.earliest_slot,
                "exhausted": self.checkpoint.exhausted,
            },
            "pages_scanned": self.pages_scanned,
            "signatures_scanned": self.signatures_scanned,
            "oldest_slot": self.oldest_slot,
            "exhausted": self.exhausted,
        }


def deep_scan_with_checkpoint(
    fetch_page: FetchPageFn,
    initial_checkpoint: Optional[HistoryCheckpoint] = None,
    max_pages: int = 10,
    page_limit: int = 100,
) -> DeepHistoryScanResult:
    """
    Deterministic deep-history scan using a checkpoint and a page fetcher.

    - fetch_page(before, limit) returns a sequence of HistoryPageEntry rows,
      ordered newest→oldest or in any order; only the minimum slot in each page
      is used to advance the checkpoint.
    - initial_checkpoint can be None (fresh scan) or an existing checkpoint
      from a prior run.
    - max_pages and page_limit bound work per invocation; callers can resume
      later with the returned checkpoint.
    """
    cp = initial_checkpoint or HistoryCheckpoint()
    pages_scanned = 0
    signatures_scanned = 0
    oldest_slot: Optional[int] = cp.earliest_slot

    while pages_scanned < max_pages and not cp.exhausted:
        before = next_before_anchor(cp)
        page = fetch_page(before, page_limit)
        if page:
            pages_scanned += 1
            signatures_scanned += len(page)
            page_min_slot = min(e.slot for e in page)
            if oldest_slot is None or page_min_slot < oldest_slot:
                oldest_slot = page_min_slot
        cp = advance_checkpoint(cp, page)
        if cp.exhausted:
            break

    return DeepHistoryScanResult(
        checkpoint=cp,
        pages_scanned=pages_scanned,
        signatures_scanned=signatures_scanned,
        oldest_slot=oldest_slot,
        exhausted=cp.exhausted,
    )


def serialize_checkpoint(cp: HistoryCheckpoint) -> dict:
    return {
        "earliest_signature": cp.earliest_signature,
        "earliest_slot": cp.earliest_slot,
        "exhausted": cp.exhausted,
    }


def deserialize_checkpoint(data: dict) -> HistoryCheckpoint:
    return HistoryCheckpoint(
        earliest_signature=data.get("earliest_signature"),
        earliest_slot=data.get("earliest_slot"),
        exhausted=bool(data.get("exhausted", False)),
        updated_at=None,
    )

