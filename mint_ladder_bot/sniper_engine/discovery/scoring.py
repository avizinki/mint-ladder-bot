"""
Discovery candidate scoring — multi-dimensional weighted model.

Scores 0.0–1.0. Returns both final_score and score_breakdown dict.

Safety: weights are normalized per source type so scores are comparable across sources.
Sources without whale/momentum dimensions are NOT penalized — their weights are
renormalized to 1.0 across applicable dimensions only.

Enrichment partial-failure penalty: if enrichment data shows unavailable authority
checks, a configurable penalty is applied to the score.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .model import DiscoveredCandidate

# Minimum score required for a candidate to be accepted.
DEFAULT_MIN_SCORE = 0.3

# Penalty applied when enrichment data is partial (unavailable checks)
ENRICHMENT_PARTIAL_PENALTY = 0.05

# Per-source weight maps. Weights must sum to 1.0.
# Dimensions: source_confidence, liquidity_signal, metadata_completeness,
#             whale_signal, momentum_signal
_SOURCE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "watchlist": {
        "source_confidence": 0.35,
        "liquidity_signal": 0.40,
        "metadata_completeness": 0.25,
    },
    "pumpfun": {
        "source_confidence": 0.35,
        "liquidity_signal": 0.40,
        "metadata_completeness": 0.25,
    },
    "test": {
        "source_confidence": 0.35,
        "liquidity_signal": 0.40,
        "metadata_completeness": 0.25,
    },
    "whale_copy": {
        "source_confidence": 0.20,
        "liquidity_signal": 0.25,
        "metadata_completeness": 0.10,
        "whale_signal": 0.45,
    },
    "momentum": {
        "source_confidence": 0.20,
        "liquidity_signal": 0.30,
        "metadata_completeness": 0.10,
        "momentum_signal": 0.40,
    },
}

# Default weights for unknown sources (same as watchlist/pumpfun)
_DEFAULT_WEIGHTS: Dict[str, float] = {
    "source_confidence": 0.35,
    "liquidity_signal": 0.40,
    "metadata_completeness": 0.25,
}


def score_candidate(
    candidate: DiscoveredCandidate,
    min_liquidity_usd: float = 5_000.0,
    high_liquidity_usd: float = 50_000.0,
    enrichment_data: Optional[Dict[str, Any]] = None,
) -> Tuple[float, Dict[str, float]]:
    """
    Score a candidate using per-source weight normalization.

    Returns (final_score, score_breakdown).
    final_score: 0.0–1.0
    score_breakdown: {dimension: weighted_contribution} for observability.

    enrichment_data: dict from CandidateEnricher.enrich(); used for partial penalty.
    """
    weights = _SOURCE_WEIGHTS.get(candidate.source_id, _DEFAULT_WEIGHTS)

    # --- Compute raw dimension values (each 0.0–1.0) ---
    dim_values: Dict[str, float] = {}

    # source_confidence: passthrough (already 0–1)
    dim_values["source_confidence"] = max(0.0, min(1.0, candidate.source_confidence))

    # liquidity_signal
    liq = candidate.liquidity_usd
    if liq is None or liq <= 0:
        dim_values["liquidity_signal"] = 0.0
    elif liq >= high_liquidity_usd:
        dim_values["liquidity_signal"] = 1.0
    elif liq >= min_liquidity_usd:
        dim_values["liquidity_signal"] = 0.5
    else:
        dim_values["liquidity_signal"] = 0.1  # some liquidity, below threshold

    # metadata_completeness
    meta_score = 0.0
    if candidate.symbol:
        meta_score += 0.5
    if candidate.deployer:
        meta_score += 0.3
    if candidate.metadata.get("name"):
        meta_score += 0.2
    dim_values["metadata_completeness"] = min(1.0, meta_score)

    # whale_signal (only meaningful for whale_copy source)
    if "whale_signal" in weights:
        signals = candidate.discovery_signals or {}
        wallet_confidence = float(signals.get("wallet_confidence", 0.0))
        buy_signal = _buy_size_signal(signals.get("buy_amount_sol"))
        dim_values["whale_signal"] = min(1.0, (wallet_confidence * 0.7) + (buy_signal * 0.3))
    else:
        dim_values["whale_signal"] = 0.0

    # momentum_signal (only meaningful for momentum source)
    if "momentum_signal" in weights:
        signals = candidate.discovery_signals or {}
        price_change = abs(float(signals.get("price_change_pct_5m", 0.0)))
        vol_signal = _volume_signal(signals.get("volume_usd_5m"))
        price_signal = min(1.0, price_change / 50.0)  # 50% change = full signal
        dim_values["momentum_signal"] = min(1.0, (price_signal * 0.5) + (vol_signal * 0.5))
    else:
        dim_values["momentum_signal"] = 0.0

    # --- Weighted sum using only applicable dimensions ---
    total_weight = 0.0
    weighted_score = 0.0
    breakdown: Dict[str, float] = {}

    for dim, weight in weights.items():
        val = dim_values.get(dim, 0.0)
        contribution = round(val * weight, 4)
        breakdown[dim] = contribution
        weighted_score += contribution
        total_weight += weight

    # Normalize (weights should already sum to 1.0 by design, but guard against float drift)
    if total_weight > 0 and abs(total_weight - 1.0) > 0.01:
        weighted_score = weighted_score / total_weight

    final_score = min(1.0, round(weighted_score, 4))

    # --- Enrichment partial penalty ---
    if enrichment_data:
        if enrichment_data.get("authority_check") == "unavailable":
            final_score = max(0.0, final_score - ENRICHMENT_PARTIAL_PENALTY)
            breakdown["enrichment_partial_penalty"] = -ENRICHMENT_PARTIAL_PENALTY

    return final_score, breakdown


def passes_score_threshold(score: float, min_score: float = DEFAULT_MIN_SCORE) -> bool:
    return score >= min_score


# ------------------------------------------------------------------
# Signal helpers
# ------------------------------------------------------------------

def _buy_size_signal(buy_sol: Any) -> float:
    """Map buy size in SOL to 0.0–1.0 signal. None → 0.0."""
    if buy_sol is None:
        return 0.0
    try:
        sol = float(buy_sol)
    except (TypeError, ValueError):
        return 0.0
    if sol <= 0:
        return 0.0
    if sol >= 10.0:
        return 1.0
    if sol >= 1.0:
        return 0.7
    if sol >= 0.1:
        return 0.4
    return 0.1


def _volume_signal(volume_usd: Any) -> float:
    """Map 5m volume in USD to 0.0–1.0 signal. None → 0.0."""
    if volume_usd is None:
        return 0.0
    try:
        vol = float(volume_usd)
    except (TypeError, ValueError):
        return 0.0
    if vol <= 0:
        return 0.0
    if vol >= 100_000:
        return 1.0
    if vol >= 10_000:
        return 0.7
    if vol >= 1_000:
        return 0.4
    return 0.1
