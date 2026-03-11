"""
Scoring function tests.

Verifies score_candidate() produces expected values for known inputs.
"""
from __future__ import annotations

from datetime import datetime, timezone

from mint_ladder_bot.sniper_engine.discovery.model import DiscoveredCandidate
from mint_ladder_bot.sniper_engine.discovery.scoring import score_candidate, passes_score_threshold, DEFAULT_MIN_SCORE


def _cand(
    symbol=None,
    deployer=None,
    source_confidence=0.0,
    liquidity_usd=None,
) -> DiscoveredCandidate:
    return DiscoveredCandidate(
        mint="A" * 44,
        source_id="test",
        source_confidence=source_confidence,
        discovered_at=datetime.now(tz=timezone.utc),
        symbol=symbol,
        liquidity_usd=liquidity_usd,
        deployer=deployer,
    )


def test_score_zero_signal_is_zero() -> None:
    cand = _cand()
    score = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert score == 0.0


def test_score_symbol_adds_0_2() -> None:
    cand = _cand(symbol="TOKEN")
    score = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert abs(score - 0.2) < 1e-6


def test_score_deployer_adds_0_1() -> None:
    cand = _cand(deployer="D" * 44)
    score = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert abs(score - 0.1) < 1e-6


def test_score_source_confidence_1_0_adds_0_3() -> None:
    cand = _cand(source_confidence=1.0)
    score = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert abs(score - 0.3) < 1e-6


def test_score_high_liquidity_adds_0_4() -> None:
    cand = _cand(liquidity_usd=100_000.0)
    score = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert abs(score - 0.4) < 1e-6


def test_score_min_liquidity_adds_0_2() -> None:
    cand = _cand(liquidity_usd=10_000.0)
    score = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert abs(score - 0.2) < 1e-6


def test_score_capped_at_1_0() -> None:
    cand = _cand(symbol="T", deployer="D" * 44, source_confidence=1.0, liquidity_usd=100_000.0)
    score = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert score <= 1.0


def test_score_all_signals_high() -> None:
    cand = _cand(symbol="T", deployer="D" * 44, source_confidence=1.0, liquidity_usd=100_000.0)
    score = score_candidate(cand)
    # symbol(0.2) + deployer(0.1) + confidence(0.3) + high_liq(0.4) = 1.0 (capped)
    assert score == 1.0


def test_passes_threshold_true() -> None:
    assert passes_score_threshold(0.5, min_score=0.3)


def test_passes_threshold_false_below() -> None:
    assert not passes_score_threshold(0.2, min_score=0.3)


def test_passes_threshold_exact_boundary() -> None:
    assert passes_score_threshold(0.3, min_score=0.3)


def test_source_confidence_clamped_to_0_1() -> None:
    from mint_ladder_bot.sniper_engine.discovery.model import make_candidate
    cand = make_candidate("A" * 44, "test", source_confidence=2.0)
    assert cand.source_confidence == 1.0
    cand2 = make_candidate("A" * 44, "test", source_confidence=-0.5)
    assert cand2.source_confidence == 0.0
