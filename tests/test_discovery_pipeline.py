"""
Discovery pipeline integration tests.

Tests the full pipeline.run() path with mocked sources.
Verifies bounded history, stats accumulation, and source breakdown.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from mint_ladder_bot.config import Config
from mint_ladder_bot.models import RuntimeState, SolBalance
from mint_ladder_bot.sniper_engine.discovery.model import DiscoveredCandidate
from mint_ladder_bot.sniper_engine.discovery.pipeline import DiscoveryPipeline


def _make_state() -> RuntimeState:
    return RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        wallet="W",
        sol=SolBalance(lamports=0, sol=0.0),
        mints={},
    )


def _make_cfg() -> Config:
    cfg = Config()
    cfg.discovery_enabled = True
    cfg.discovery_review_only = True
    cfg.sniper_min_score = 0.0
    cfg.sniper_min_liquidity_sol_equiv = 0.0
    cfg.discovery_max_candidates_per_cycle = 20
    cfg.discovery_max_history = 5
    cfg.discovery_max_rejected = 5
    return cfg


def _patch_fetch(candidates):
    return patch(
        "mint_ladder_bot.sniper_engine.discovery.pipeline.fetch_from_sources",
        return_value=candidates,
    )


def _cand(n: int, source_id: str = "test") -> DiscoveredCandidate:
    mint = str(n) * 44
    return DiscoveredCandidate(
        mint=mint[:44],
        source_id=source_id,
        source_confidence=1.0,
        discovered_at=datetime.now(tz=timezone.utc),
        symbol=f"T{n}",
        liquidity_usd=10_000.0,
    )


# ---------------------------------------------------------------------------
# Stats accumulation
# ---------------------------------------------------------------------------

def test_stats_total_discovered_increments() -> None:
    cfg = _make_cfg()
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)

    candidates = [_cand(i) for i in range(5)]
    with _patch_fetch(candidates):
        pipeline.run(state=state)

    assert state.discovery_stats.total_discovered == 5
    assert state.discovery_stats.total_accepted == 5
    assert state.discovery_stats.total_rejected == 0


def test_stats_by_source_breakdown() -> None:
    cfg = _make_cfg()
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)

    candidates = [
        _cand(1, source_id="pumpfun"),
        _cand(2, source_id="pumpfun"),
        _cand(3, source_id="watchlist"),
    ]
    with _patch_fetch(candidates):
        pipeline.run(state=state)

    assert state.discovery_stats.by_source.get("pumpfun", 0) == 2
    assert state.discovery_stats.by_source.get("watchlist", 0) == 1


# ---------------------------------------------------------------------------
# Bounded history
# ---------------------------------------------------------------------------

def test_recent_candidates_bounded_to_max_history() -> None:
    cfg = _make_cfg()
    cfg.discovery_max_history = 3
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)

    # Run twice, 3 candidates each
    candidates_a = [_cand(i) for i in range(3)]
    candidates_b = [_cand(i + 10) for i in range(3)]

    with _patch_fetch(candidates_a):
        pipeline.run(state=state)
    with _patch_fetch(candidates_b):
        pipeline.run(state=state)

    # Max 3 in recent list (oldest trimmed)
    assert len(state.discovery_recent_candidates) <= 3


def test_rejected_candidates_bounded_to_max_rejected() -> None:
    cfg = _make_cfg()
    cfg.discovery_max_rejected = 2
    cfg.sniper_min_score = 0.99  # reject everything (no candidate has score >= 0.99 without high signal)
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)

    candidates = [_cand(i) for i in range(5)]
    with _patch_fetch(candidates):
        pipeline.run(state=state)

    assert len(state.discovery_rejected_candidates) <= 2


# ---------------------------------------------------------------------------
# Empty source → no records
# ---------------------------------------------------------------------------

def test_no_candidates_no_records() -> None:
    cfg = _make_cfg()
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)

    with _patch_fetch([]):
        records = pipeline.run(state=state)

    assert records == []
    assert state.discovery_stats.total_discovered == 0
    assert state.discovery_recent_candidates == []
    assert state.discovery_rejected_candidates == []


# ---------------------------------------------------------------------------
# Records returned by run() match state
# ---------------------------------------------------------------------------

def test_run_returns_records_created_this_cycle() -> None:
    cfg = _make_cfg()
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)

    candidates = [_cand(1), _cand(2)]
    with _patch_fetch(candidates):
        records = pipeline.run(state=state)

    assert len(records) == 2
    record_ids = {r.record_id for r in records}
    state_ids = {r.record_id for r in state.discovery_recent_candidates + state.discovery_rejected_candidates}
    assert record_ids.issubset(state_ids)
