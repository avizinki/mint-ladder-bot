"""
Scoring function tests.

Verifies score_candidate() produces expected values for known inputs under the
multi-dimensional weighted model.

Score returns (float, dict). For "test" source_id the weights are:
  source_confidence=0.35, liquidity_signal=0.40, metadata_completeness=0.25
"""
from __future__ import annotations

from datetime import datetime, timezone

from mint_ladder_bot.sniper_engine.discovery.model import DiscoveredCandidate, make_candidate
from mint_ladder_bot.sniper_engine.discovery.scoring import score_candidate, passes_score_threshold, DEFAULT_MIN_SCORE


def _cand(
    symbol=None,
    deployer=None,
    source_confidence=0.0,
    liquidity_usd=None,
    source_id="test",
) -> DiscoveredCandidate:
    return DiscoveredCandidate(
        mint="A" * 44,
        source_id=source_id,
        source_confidence=source_confidence,
        discovered_at=datetime.now(tz=timezone.utc),
        symbol=symbol,
        liquidity_usd=liquidity_usd,
        deployer=deployer,
    )


def test_score_returns_tuple() -> None:
    """score_candidate returns (float, dict)."""
    cand = _cand()
    result = score_candidate(cand)
    assert isinstance(result, tuple)
    assert len(result) == 2
    score, breakdown = result
    assert isinstance(score, float)
    assert isinstance(breakdown, dict)


def test_score_zero_signal_is_zero() -> None:
    cand = _cand()
    score, _ = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert score == 0.0


def test_score_symbol_contributes_metadata() -> None:
    """Symbol = 0.5 metadata value × 0.25 weight = 0.125 for 'test' source."""
    cand = _cand(symbol="TOKEN")
    score, breakdown = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert abs(score - 0.125) < 1e-4
    assert "metadata_completeness" in breakdown


def test_score_source_confidence_1_0() -> None:
    """source_confidence=1.0 × 0.35 weight = 0.35 for 'test' source."""
    cand = _cand(source_confidence=1.0)
    score, _ = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert abs(score - 0.35) < 1e-4


def test_score_high_liquidity() -> None:
    """High liquidity (>=50k) × 0.40 weight = 0.40 for 'test' source."""
    cand = _cand(liquidity_usd=100_000.0)
    score, _ = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert abs(score - 0.40) < 1e-4


def test_score_mid_liquidity() -> None:
    """Mid liquidity (>=5k, <50k) gives 0.5 liquidity signal × 0.40 = 0.20."""
    cand = _cand(liquidity_usd=10_000.0)
    score, _ = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert abs(score - 0.20) < 1e-4


def test_score_capped_at_1_0() -> None:
    cand = _cand(symbol="T", deployer="D" * 44, source_confidence=1.0, liquidity_usd=100_000.0)
    score, _ = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert score <= 1.0


def test_score_all_signals_high() -> None:
    """symbol+deployer metadata=0.8 × 0.25, confidence=1.0 × 0.35, high_liq=1.0 × 0.40 = 0.95."""
    cand = _cand(symbol="T", deployer="D" * 44, source_confidence=1.0, liquidity_usd=100_000.0)
    score, breakdown = score_candidate(cand)
    # 0.8*0.25 + 1.0*0.35 + 1.0*0.40 = 0.20 + 0.35 + 0.40 = 0.95
    assert abs(score - 0.95) < 1e-4
    assert score <= 1.0


def test_score_breakdown_keys_match_source() -> None:
    """Breakdown keys must match the source weight map dimensions."""
    cand = _cand()
    _, breakdown = score_candidate(cand)
    assert "source_confidence" in breakdown
    assert "liquidity_signal" in breakdown
    assert "metadata_completeness" in breakdown


def test_score_whale_copy_uses_whale_signal_weight() -> None:
    """whale_copy source uses its own weight map including whale_signal dimension."""
    cand = _cand(source_id="whale_copy", source_confidence=0.8, liquidity_usd=20_000.0)
    cand.discovery_signals = {"wallet_confidence": 0.8, "buy_amount_sol": 2.0}
    score, breakdown = score_candidate(cand, min_liquidity_usd=5_000.0, high_liquidity_usd=50_000.0)
    assert "whale_signal" in breakdown
    assert breakdown["whale_signal"] > 0.0
    assert score > 0.0


def test_passes_threshold_true() -> None:
    assert passes_score_threshold(0.5, min_score=0.3)


def test_passes_threshold_false_below() -> None:
    assert not passes_score_threshold(0.2, min_score=0.3)


def test_passes_threshold_exact_boundary() -> None:
    assert passes_score_threshold(0.3, min_score=0.3)


def test_source_confidence_clamped_to_0_1() -> None:
    cand = make_candidate("A" * 44, "test", source_confidence=2.0)
    assert cand.source_confidence == 1.0
    cand2 = make_candidate("A" * 44, "test", source_confidence=-0.5)
    assert cand2.source_confidence == 0.0
