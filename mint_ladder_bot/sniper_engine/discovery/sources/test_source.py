"""
Test discovery source adapter.

Wraps existing launch_detector._detect_test_mints (SNIPER_TEST_MINTS env var).
source_id: "test"
source_confidence: 1.0 (operator-specified; treated as high-confidence for pipeline testing)

This source is purely for integration testing. It reads SNIPER_TEST_MINTS (comma-sep mints).
Returns empty list when SNIPER_TEST_MINTS is not set.
"""
from __future__ import annotations

import logging
from typing import List

from ..model import DiscoveredCandidate, make_candidate
from ..registry import register_source

logger = logging.getLogger(__name__)

SOURCE_ID = "test"
SOURCE_CONFIDENCE = 1.0


def _fetch(limit: int) -> List[DiscoveredCandidate]:
    try:
        from ...launch_detector import _detect_test_mints
        launches = _detect_test_mints(limit=limit)
    except Exception as e:
        logger.debug("test_source fetch failed: %s", e)
        return []
    out: List[DiscoveredCandidate] = []
    for lc in launches:
        meta = lc.metadata or {}
        out.append(make_candidate(
            mint=lc.mint,
            source_id=SOURCE_ID,
            source_confidence=SOURCE_CONFIDENCE,
            symbol=meta.get("symbol"),
            discovered_at=lc.detected_at,
        ))
    return out


def register() -> None:
    register_source(SOURCE_ID, _fetch)
