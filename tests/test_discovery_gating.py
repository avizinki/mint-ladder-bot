"""
Discovery gating tests — review_only and sniper_mode interaction.

Rules verified:
1. discovery_review_only=True (default): accepted candidates NOT enqueued
2. discovery_review_only=False + sniper_mode=live: enqueue_fn called for accepted
3. discovery_review_only=False + sniper_mode=disabled: NOT enqueued
4. discovery_review_only=False + sniper_mode=paper: enqueue_fn called
5. sniper_mode=disabled: process_candidate_queue is no-op at service level
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from mint_ladder_bot.config import Config
from mint_ladder_bot.models import RuntimeState, SolBalance
from mint_ladder_bot.sniper_engine.discovery.model import DiscoveredCandidate
from mint_ladder_bot.sniper_engine.discovery.pipeline import DiscoveryPipeline


FAKE_MINT = "G" * 44


def _make_state() -> RuntimeState:
    return RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        wallet="W",
        sol=SolBalance(lamports=0, sol=0.0),
        mints={},
    )


def _make_cfg(review_only: bool = True, mode: str = "live") -> Config:
    cfg = Config()
    cfg.discovery_enabled = True
    cfg.discovery_review_only = review_only
    cfg.sniper_min_score = 0.0  # accept all
    cfg.sniper_min_liquidity_sol_equiv = 0.0
    cfg.discovery_max_candidates_per_cycle = 10
    cfg.sniper_enabled = True
    cfg.sniper_mode = mode
    return cfg


def _good_candidate() -> DiscoveredCandidate:
    return DiscoveredCandidate(
        mint=FAKE_MINT,
        source_id="test",
        source_confidence=1.0,
        discovered_at=datetime.now(tz=timezone.utc),
        symbol="TOKEN",
        liquidity_usd=50_000.0,
    )


def _patch_fetch(candidates):
    return patch(
        "mint_ladder_bot.sniper_engine.discovery.pipeline.fetch_from_sources",
        return_value=candidates,
    )


# ---------------------------------------------------------------------------
# 1. review_only=True: no enqueue
# ---------------------------------------------------------------------------

def test_review_only_does_not_call_enqueue_fn() -> None:
    cfg = _make_cfg(review_only=True, mode="live")
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)

    enqueue_fn = MagicMock(return_value=(True, None, 1))
    with _patch_fetch([_good_candidate()]):
        pipeline.run(state=state, enqueue_fn=enqueue_fn)

    enqueue_fn.assert_not_called()
    # Candidate should be in recent history as "accepted" (not "enqueued")
    assert len(state.discovery_recent_candidates) == 1
    assert state.discovery_recent_candidates[0].outcome == "accepted"


# ---------------------------------------------------------------------------
# 2. review_only=False + live: enqueue_fn IS called
# ---------------------------------------------------------------------------

def test_not_review_only_live_calls_enqueue_fn() -> None:
    cfg = _make_cfg(review_only=False, mode="live")
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)

    enqueue_fn = MagicMock(return_value=(True, None, 1))
    with _patch_fetch([_good_candidate()]):
        pipeline.run(state=state, enqueue_fn=enqueue_fn)

    enqueue_fn.assert_called_once()
    assert state.discovery_stats.total_enqueued == 1
    assert state.discovery_recent_candidates[0].outcome == "enqueued"


# ---------------------------------------------------------------------------
# 3. service-level: review_only=False but sniper_mode=disabled → no enqueue_fn passed
# ---------------------------------------------------------------------------

def test_service_disabled_mode_no_enqueue() -> None:
    """SniperService with mode=disabled never passes enqueue_fn."""
    from mint_ladder_bot.sniper_engine.service import SniperService

    cfg = _make_cfg(review_only=False, mode="disabled")
    cfg.sniper_enabled = False  # disabled
    state = _make_state()
    svc = SniperService(config=cfg, state=state)

    # process_candidate_queue must be no-op when service not enabled
    svc.process_candidate_queue()
    assert state.discovery_stats.total_discovered == 0


# ---------------------------------------------------------------------------
# 4. review_only=False + paper: enqueue_fn called
# ---------------------------------------------------------------------------

def test_not_review_only_paper_calls_enqueue_fn() -> None:
    cfg = _make_cfg(review_only=False, mode="paper")
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)

    enqueue_fn = MagicMock(return_value=(True, None, 1))
    with _patch_fetch([_good_candidate()]):
        pipeline.run(state=state, enqueue_fn=enqueue_fn)

    enqueue_fn.assert_called_once()


# ---------------------------------------------------------------------------
# 5. enqueue_fn returns rejected: outcome updated to rejected
# ---------------------------------------------------------------------------

def test_enqueue_fn_rejection_updates_outcome() -> None:
    cfg = _make_cfg(review_only=False, mode="live")
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)

    # enqueue returns failure (e.g. cooldown)
    enqueue_fn = MagicMock(return_value=(False, "cooldown_blocked", 0))
    with _patch_fetch([_good_candidate()]):
        pipeline.run(state=state, enqueue_fn=enqueue_fn)

    # Record ends up in rejected list
    assert len(state.discovery_rejected_candidates) == 1
    assert state.discovery_rejected_candidates[0].rejection_reason == "cooldown_blocked"


# ---------------------------------------------------------------------------
# 6. Max candidates per cycle is respected
# ---------------------------------------------------------------------------

def test_max_candidates_per_cycle_limits_processed() -> None:
    cfg = _make_cfg(review_only=True)
    cfg.discovery_max_candidates_per_cycle = 2
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)

    candidates = [
        DiscoveredCandidate(mint="A" * 44, source_id="test", source_confidence=1.0,
                            discovered_at=datetime.now(tz=timezone.utc), symbol="A"),
        DiscoveredCandidate(mint="B" * 44, source_id="test", source_confidence=1.0,
                            discovered_at=datetime.now(tz=timezone.utc), symbol="B"),
        DiscoveredCandidate(mint="C" * 44, source_id="test", source_confidence=1.0,
                            discovered_at=datetime.now(tz=timezone.utc), symbol="C"),
    ]
    with _patch_fetch(candidates):
        records = pipeline.run(state=state)

    assert len(records) == 2
    total = len(state.discovery_recent_candidates) + len(state.discovery_rejected_candidates)
    assert total == 2
