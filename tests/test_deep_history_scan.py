from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence, Tuple

from mint_ladder_bot.deep_history_scan import (
    DeepHistoryScanResult,
    deep_scan_with_checkpoint,
    deserialize_checkpoint,
    serialize_checkpoint,
)
from mint_ladder_bot.history_checkpoint import HistoryCheckpoint, HistoryPageEntry


class _FakePager:
    """
    Fake paginated history:

    Slots: 100, 90, 80, 70, 60
    Page size: 2

    Pagination rule:
    - before=None -> [100, 90]
    - before=sig_90 -> [80, 70]
    - before=sig_70 -> [60]
    - before=sig_60 or older -> []
    """

    def __init__(self) -> None:
        # Ordered newest -> oldest
        self.entries: List[HistoryPageEntry] = [
            HistoryPageEntry("sig_100", 100),
            HistoryPageEntry("sig_90", 90),
            HistoryPageEntry("sig_80", 80),
            HistoryPageEntry("sig_70", 70),
            HistoryPageEntry("sig_60", 60),
        ]
        self.calls: List[Tuple[Optional[str], int]] = []

    def fetch(self, before: Optional[str], limit: int) -> Sequence[HistoryPageEntry]:
        self.calls.append((before, limit))
        if before is None:
            return self.entries[0:2]
        if before == "sig_90":
            return self.entries[2:4]
        if before == "sig_70":
            return self.entries[4:5]
        return []


def test_multi_page_scan_from_no_checkpoint():
    pager = _FakePager()

    res: DeepHistoryScanResult = deep_scan_with_checkpoint(
        fetch_page=pager.fetch,
        initial_checkpoint=None,
        max_pages=10,
        page_limit=2,
    )

    assert res.pages_scanned == 3
    assert res.signatures_scanned == 5
    assert res.oldest_slot == 60
    assert res.checkpoint.earliest_slot == 60
    assert res.checkpoint.earliest_signature == "sig_60"
    assert res.checkpoint.exhausted is True
    # Pagination anchors should have followed sig_90 then sig_70 then sig_60 (empty).
    assert pager.calls[0][0] is None
    assert pager.calls[1][0] == "sig_90"
    assert pager.calls[2][0] == "sig_70"


def test_resumed_scan_from_checkpoint():
    pager = _FakePager()
    # First run: only 1 page.
    res1: DeepHistoryScanResult = deep_scan_with_checkpoint(
        fetch_page=pager.fetch,
        initial_checkpoint=None,
        max_pages=1,
        page_limit=2,
    )
    assert res1.pages_scanned == 1
    assert res1.checkpoint.earliest_slot == 90
    assert res1.checkpoint.exhausted is False
    assert pager.calls == [(None, 2)]

    # Second run: resume from checkpoint.
    pager2 = _FakePager()
    res2: DeepHistoryScanResult = deep_scan_with_checkpoint(
        fetch_page=pager2.fetch,
        initial_checkpoint=res1.checkpoint,
        max_pages=10,
        page_limit=2,
    )
    assert res2.pages_scanned == 2
    assert res2.checkpoint.earliest_slot == 60
    assert res2.checkpoint.exhausted is True
    assert pager2.calls[0][0] == "sig_90"
    assert pager2.calls[1][0] == "sig_70"


def test_exhausted_history_is_stable_on_resume():
    pager = _FakePager()
    res1 = deep_scan_with_checkpoint(pager.fetch, None, max_pages=10, page_limit=2)
    assert res1.checkpoint.exhausted is True

    pager2 = _FakePager()
    res2 = deep_scan_with_checkpoint(
        fetch_page=pager2.fetch,
        initial_checkpoint=res1.checkpoint,
        max_pages=10,
        page_limit=2,
    )
    # No more pages should be fetched when exhausted.
    assert res2.pages_scanned == 0
    assert res2.signatures_scanned == 0
    assert pager2.calls == []
    assert res2.checkpoint == res1.checkpoint


def test_no_duplicate_page_processing_across_resume():
    pager = _FakePager()
    res1 = deep_scan_with_checkpoint(pager.fetch, None, max_pages=1, page_limit=2)
    assert pager.calls == [(None, 2)]

    pager2 = _FakePager()
    res2 = deep_scan_with_checkpoint(pager2.fetch, res1.checkpoint, max_pages=1, page_limit=2)
    # Only the second page (older than checkpoint) should be fetched.
    assert pager2.calls == [("sig_90", 2)]
    assert res2.checkpoint.earliest_slot == 70


def test_checkpoint_roundtrip_serialization():
    pager = _FakePager()
    res = deep_scan_with_checkpoint(pager.fetch, None, max_pages=2, page_limit=2)
    data = serialize_checkpoint(res.checkpoint)
    cp2 = deserialize_checkpoint(json.loads(json.dumps(data)))
    assert cp2.earliest_signature == res.checkpoint.earliest_signature
    assert cp2.earliest_slot == res.checkpoint.earliest_slot
    assert cp2.exhausted == res.checkpoint.exhausted

