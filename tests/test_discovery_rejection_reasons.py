"""
Rejection reason tracking tests.

Ensures:
- Rejection reasons map to stable reason codes
- Reason counts accumulate in DiscoveryStats.by_rejection_reason
- Score-blocked candidates get REASON_SCORE_BLOCKED
- Filter-blocked candidates get the token_filter reason code
"""
from __future__ import annotations

from datetime import datetime, timezone

from mint_ladder_bot.config import Config
from mint_ladder_bot.models import RuntimeState, SolBalance
from mint_ladder_bot.sniper_engine.discovery.model import DiscoveredCandidate
from mint_ladder_bot.sniper_engine.discovery.pipeline import (
    DiscoveryPipeline,
    REASON_SCORE_BLOCKED,
)
from mint_ladder_bot.sniper_engine.token_filter import (
    REASON_LIQUIDITY_BELOW_THRESHOLD,
    REASON_BLOCKLIST,
)


def _make_state() -> RuntimeState:
    return RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        wallet="WALLET",
        sol=SolBalance(lamports=0, sol=0.0),
        mints={},
    )


def _make_cfg(min_score: float = 0.5, min_liq: float = 5000.0) -> Config:
    cfg = Config()
    cfg.discovery_enabled = True
    cfg.discovery_review_only = True
    cfg.discovery_min_score = min_score
    cfg.sniper_min_liquidity_sol_equiv = min_liq
    cfg.discovery_max_candidates_per_cycle = 20
    return cfg


def _cand(mint: str = "A" * 44, symbol: str = "X", liquidity_usd: float = None, source_confidence: float = 0.0) -> DiscoveredCandidate:
    return DiscoveredCandidate(
        mint=mint,
        source_id="test",
        source_confidence=source_confidence,
        discovered_at=datetime.now(tz=timezone.utc),
        symbol=symbol,
        liquidity_usd=liquidity_usd,
    )


def _run_one(candidate, state, min_score=0.0, min_liq=0.0):
    cfg = _make_cfg(min_score=min_score, min_liq=min_liq)
    pipeline = DiscoveryPipeline(cfg)
    return pipeline._process_one(candidate, state, min_score=min_score, min_liq_usd=min_liq)


# ---------------------------------------------------------------------------
# Score-blocked
# ---------------------------------------------------------------------------

def test_low_score_produces_score_blocked_reason() -> None:
    state = _make_state()
    # No symbol, no deployer, no liquidity, zero source_confidence → score = 0.0
    cand = _cand(symbol=None, liquidity_usd=None, source_confidence=0.0)
    record, _ = _run_one(cand, state, min_score=0.5, min_liq=0.0)

    assert record.outcome == "rejected"
    assert record.rejection_reason == REASON_SCORE_BLOCKED


def test_score_blocked_increments_stats() -> None:
    state = _make_state()
    cand = _cand(symbol=None, liquidity_usd=None, source_confidence=0.0)

    cfg = _make_cfg(min_score=0.5)
    pipeline = DiscoveryPipeline(cfg)

    from unittest.mock import patch
    with patch("mint_ladder_bot.sniper_engine.discovery.pipeline.fetch_from_sources", return_value=[cand]):
        pipeline.run(state=state)

    assert state.discovery_stats.by_rejection_reason.get(REASON_SCORE_BLOCKED, 0) >= 1
    assert state.discovery_stats.total_rejected >= 1


# ---------------------------------------------------------------------------
# Liquidity filter rejection
# ---------------------------------------------------------------------------

def test_low_liquidity_produces_filter_reason() -> None:
    state = _make_state()
    cand = _cand(liquidity_usd=100.0, source_confidence=1.0)  # below 5000 threshold
    record, _ = _run_one(cand, state, min_score=0.0, min_liq=5_000.0)

    assert record.outcome == "rejected"
    assert record.rejection_reason == REASON_LIQUIDITY_BELOW_THRESHOLD


def test_liquidity_filter_increments_stats() -> None:
    state = _make_state()
    cand = _cand(liquidity_usd=100.0, source_confidence=1.0)

    cfg = _make_cfg(min_score=0.0, min_liq=5_000.0)
    pipeline = DiscoveryPipeline(cfg)

    from unittest.mock import patch
    with patch("mint_ladder_bot.sniper_engine.discovery.pipeline.fetch_from_sources", return_value=[cand]):
        pipeline.run(state=state)

    reason_counts = state.discovery_stats.by_rejection_reason
    assert reason_counts.get(REASON_LIQUIDITY_BELOW_THRESHOLD, 0) >= 1


# ---------------------------------------------------------------------------
# Blocklist reason
# ---------------------------------------------------------------------------

def test_blocklist_mint_is_rejected() -> None:
    """Mint on blocklist produces REASON_BLOCKLIST via token_filter."""
    state = _make_state()
    cand = _cand(symbol="SCAM", liquidity_usd=100_000.0, source_confidence=1.0)

    cfg = _make_cfg(min_score=0.0, min_liq=0.0)
    pipeline = DiscoveryPipeline(cfg)

    from unittest.mock import patch
    # Patch filter_candidate to simulate blocklist hit
    from mint_ladder_bot.sniper_engine.token_filter import FilterResult
    with patch("mint_ladder_bot.sniper_engine.discovery.pipeline.filter_candidate") as mock_filter:
        mock_filter.return_value = FilterResult(False, REASON_BLOCKLIST, {})
        record, _ = pipeline._process_one(cand, state, min_score=0.0, min_liq_usd=0.0)

    assert record.outcome == "rejected"
    assert record.rejection_reason == REASON_BLOCKLIST


# ---------------------------------------------------------------------------
# Accepted candidate (all passes)
# ---------------------------------------------------------------------------

def test_accepted_candidate_produces_correct_record() -> None:
    state = _make_state()
    # High confidence + symbol + liquidity → should pass with min_score=0.0
    cand = _cand(symbol="TOKEN", liquidity_usd=50_000.0, source_confidence=0.8)

    cfg = _make_cfg(min_score=0.0, min_liq=0.0)
    pipeline = DiscoveryPipeline(cfg)
    record, should_enqueue = pipeline._process_one(cand, state, min_score=0.0, min_liq_usd=0.0)

    assert record.outcome == "accepted"
    assert record.rejection_reason is None
    assert record.score is not None and record.score > 0.0
    # review_only=True so should_enqueue=False even though accepted
    assert not should_enqueue


# ---------------------------------------------------------------------------
# Multiple rejections track all reasons
# ---------------------------------------------------------------------------

def test_multiple_rejection_reasons_all_tracked() -> None:
    state = _make_state()
    mint_a = "A" * 44
    mint_b = "B" * 44

    # Mint A: score_blocked (no signal)
    cand_a = DiscoveredCandidate(mint=mint_a, source_id="test", source_confidence=0.0,
                                  discovered_at=datetime.now(tz=timezone.utc), symbol=None, liquidity_usd=None)
    # Mint B: liquidity filter (too low)
    cand_b = DiscoveredCandidate(mint=mint_b, source_id="test", source_confidence=1.0,
                                  discovered_at=datetime.now(tz=timezone.utc), symbol="B", liquidity_usd=100.0)

    cfg = _make_cfg(min_score=0.5, min_liq=5_000.0)
    pipeline = DiscoveryPipeline(cfg)

    from unittest.mock import patch
    with patch("mint_ladder_bot.sniper_engine.discovery.pipeline.fetch_from_sources", return_value=[cand_a, cand_b]):
        pipeline.run(state=state)

    reasons = state.discovery_stats.by_rejection_reason
    assert reasons.get(REASON_SCORE_BLOCKED, 0) >= 1
    assert reasons.get(REASON_LIQUIDITY_BELOW_THRESHOLD, 0) >= 1
    assert state.discovery_stats.total_rejected >= 2
