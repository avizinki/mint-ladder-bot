"""
Runtime discovery candidate model.

DiscoveredCandidate: ephemeral, in-memory, not persisted.
  Created by source adapters, enriched by the pipeline.
  Consumed by scoring and gating. Never stored in state.

Use DiscoveredCandidateRecord (models.py) for persisted snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class DiscoveredCandidate:
    """
    Normalized discovery candidate — ephemeral, in-memory only.

    source_id: stable identifier e.g. "pumpfun" | "watchlist" | "whale_copy" | "test"
    source_confidence: 0.0–1.0 hint from the source (not a risk score)
    score: filled by scoring.score_candidate(); None until scored
    discovery_signals: structured signal data from the source adapter
    """

    mint: str
    source_id: str
    source_confidence: float
    discovered_at: datetime
    symbol: Optional[str] = None
    liquidity_usd: Optional[float] = None
    deployer: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    score: Optional[float] = None
    discovery_signals: Dict[str, Any] = field(default_factory=dict)


def make_candidate(
    mint: str,
    source_id: str,
    source_confidence: float,
    symbol: Optional[str] = None,
    liquidity_usd: Optional[float] = None,
    deployer: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    discovered_at: Optional[datetime] = None,
    discovery_signals: Optional[Dict[str, Any]] = None,
) -> DiscoveredCandidate:
    """Convenience constructor with defaults."""
    return DiscoveredCandidate(
        mint=mint.strip(),
        source_id=source_id,
        source_confidence=max(0.0, min(1.0, source_confidence)),
        discovered_at=discovered_at or datetime.now(tz=timezone.utc),
        symbol=symbol,
        liquidity_usd=liquidity_usd,
        deployer=deployer,
        metadata=metadata or {},
        discovery_signals=discovery_signals or {},
    )
