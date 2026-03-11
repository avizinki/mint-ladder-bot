"""
Discovery candidate scoring.

Produces a 0.0–1.0 score from available signal.
No ML; purely heuristic. Score is used for accept/reject threshold in pipeline.
"""
from __future__ import annotations

from typing import Optional

from .model import DiscoveredCandidate


# Minimum score required for a candidate to be accepted (forwarded to enqueue gate).
DEFAULT_MIN_SCORE = 0.3


def score_candidate(
    candidate: DiscoveredCandidate,
    min_liquidity_usd: float = 5_000.0,
    high_liquidity_usd: float = 50_000.0,
) -> float:
    """
    Return a heuristic score 0.0–1.0.

    Components:
    - metadata completeness: symbol present → +0.2
    - deployer present → +0.1
    - source confidence (passthrough) → up to +0.3
    - liquidity signal → up to +0.4
      - None / zero → 0.0
      - >= min_liquidity_usd → 0.2
      - >= high_liquidity_usd → 0.4

    Score is intentionally simple and conservative.
    Higher score = more signal, not a risk guarantee.
    """
    score = 0.0

    # Metadata completeness
    if candidate.symbol:
        score += 0.2
    if candidate.deployer:
        score += 0.1

    # Source confidence (0–1 mapped to 0–0.3)
    score += candidate.source_confidence * 0.3

    # Liquidity signal
    liq = candidate.liquidity_usd
    if liq is not None and liq > 0:
        if liq >= high_liquidity_usd:
            score += 0.4
        elif liq >= min_liquidity_usd:
            score += 0.2
        else:
            score += 0.05  # some liquidity is better than none

    return min(1.0, round(score, 4))


def passes_score_threshold(score: float, min_score: float = DEFAULT_MIN_SCORE) -> bool:
    return score >= min_score
