"""
Pumpfun discovery source adapter.

Wraps existing sniper_engine.pumpfun_source → DiscoveredCandidate.
source_id: "pumpfun"
source_confidence: 0.5 (moderate; new token, bonding curve signal)
"""
from __future__ import annotations

import logging
from typing import List

from ..model import DiscoveredCandidate, make_candidate
from ..registry import register_source

logger = logging.getLogger(__name__)

SOURCE_ID = "pumpfun"
SOURCE_CONFIDENCE = 0.5


def _fetch(limit: int) -> List[DiscoveredCandidate]:
    try:
        from ...pumpfun_source import fetch_pumpfun_launches
        launches = fetch_pumpfun_launches(limit=limit)
    except Exception as e:
        logger.debug("pumpfun_adapter fetch failed: %s", e)
        return []
    out: List[DiscoveredCandidate] = []
    for lc in launches:
        meta = lc.metadata or {}
        out.append(make_candidate(
            mint=lc.mint,
            source_id=SOURCE_ID,
            source_confidence=SOURCE_CONFIDENCE,
            symbol=meta.get("symbol"),
            liquidity_usd=meta.get("liquidity_usd"),
            deployer=meta.get("deployer"),
            metadata=dict(meta),
            discovered_at=lc.detected_at,
        ))
    return out


def register() -> None:
    register_source(SOURCE_ID, _fetch)
