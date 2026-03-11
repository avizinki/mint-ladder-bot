"""
Stage 3 discovery tests — per-source review-only overrides and operator approval provenance.

Verifies:
1. _resolve_source_review_only: per-source override takes precedence over global
2. Per-source override=False: only that source's candidates get auto-enqueued
3. approve_discovery_candidate: happy path — provenance fields set correctly
4. approve_discovery_candidate: duplicate approval blocked (already_enqueued)
5. approve_discovery_candidate: missing mint blocked (not_found_or_not_accepted)
6. approve_discovery_candidate: already-enqueued outcome blocked
7. Global review_only=True default preserved when no override
8. Dashboard review_only_overrides dict populated from env
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from mint_ladder_bot.config import Config
from mint_ladder_bot.models import DiscoveredCandidateRecord, RuntimeState, SolBalance
from mint_ladder_bot.sniper_engine.discovery.model import DiscoveredCandidate
from mint_ladder_bot.sniper_engine.discovery.pipeline import (
    DiscoveryPipeline,
    _resolve_source_review_only,
)
from mint_ladder_bot.sniper_engine.service import SniperService


MINT_A = "A" * 44
MINT_B = "B" * 44


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
    cfg.discovery_min_score = 0.0
    cfg.sniper_min_liquidity_sol_equiv = 0.0
    cfg.discovery_max_candidates_per_cycle = 10
    cfg.sniper_enabled = True
    cfg.sniper_mode = mode
    cfg.discovery_review_only_watchlist = None
    cfg.discovery_review_only_pumpfun = None
    cfg.discovery_review_only_whale_copy = None
    cfg.discovery_review_only_momentum = None
    return cfg


def _good_candidate(mint: str = MINT_A, source_id: str = "test") -> DiscoveredCandidate:
    return DiscoveredCandidate(
        mint=mint,
        source_id=source_id,
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


def _accepted_record(mint: str = MINT_A, source_id: str = "watchlist") -> DiscoveredCandidateRecord:
    return DiscoveredCandidateRecord(
        record_id="rec-1",
        mint=mint,
        source_id=source_id,
        source_confidence=0.8,
        discovered_at=datetime.now(tz=timezone.utc),
        outcome="accepted",
        score=0.7,
    )


# ---------------------------------------------------------------------------
# 1. _resolve_source_review_only — override resolution
# ---------------------------------------------------------------------------

def test_resolve_uses_global_when_no_override() -> None:
    cfg = _make_cfg(review_only=True)
    assert _resolve_source_review_only(cfg, "watchlist") is True

    cfg2 = _make_cfg(review_only=False)
    assert _resolve_source_review_only(cfg2, "watchlist") is False


def test_resolve_per_source_override_false_overrides_global_true() -> None:
    cfg = _make_cfg(review_only=True)
    cfg.discovery_review_only_watchlist = False
    assert _resolve_source_review_only(cfg, "watchlist") is False


def test_resolve_per_source_override_true_overrides_global_false() -> None:
    cfg = _make_cfg(review_only=False)
    cfg.discovery_review_only_whale_copy = True
    assert _resolve_source_review_only(cfg, "whale_copy") is True


def test_resolve_override_only_affects_named_source() -> None:
    """Watchlist override=False does not affect pumpfun which still uses global."""
    cfg = _make_cfg(review_only=True)
    cfg.discovery_review_only_watchlist = False
    # pumpfun has no override → falls back to global (True)
    assert _resolve_source_review_only(cfg, "pumpfun") is True
    assert _resolve_source_review_only(cfg, "watchlist") is False


def test_resolve_unknown_source_uses_global() -> None:
    cfg = _make_cfg(review_only=True)
    assert _resolve_source_review_only(cfg, "unknown_future_source") is True


# ---------------------------------------------------------------------------
# 2. Per-source override in pipeline: only that source auto-enqueues
# ---------------------------------------------------------------------------

def test_per_source_override_only_enqueues_matching_source() -> None:
    """
    global review_only=True, watchlist override=False.
    Watchlist candidate gets enqueued; pumpfun candidate does not.
    """
    cfg = _make_cfg(review_only=True)
    cfg.discovery_review_only_watchlist = False
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)

    watchlist_cand = _good_candidate(MINT_A, source_id="watchlist")
    pumpfun_cand = _good_candidate(MINT_B, source_id="pumpfun")

    enqueue_fn = MagicMock(return_value=(True, None, 1))
    with _patch_fetch([watchlist_cand, pumpfun_cand]):
        pipeline.run(state=state, enqueue_fn=enqueue_fn)

    # enqueue_fn called exactly once (for watchlist)
    enqueue_fn.assert_called_once()
    call_mint = enqueue_fn.call_args[0][0]
    assert call_mint == MINT_A

    # Watchlist candidate enqueued, pumpfun accepted (review-only)
    outcomes = {r.mint: r.outcome for r in state.discovery_recent_candidates}
    assert outcomes.get(MINT_A) == "enqueued"
    assert outcomes.get(MINT_B) == "accepted"


def test_per_source_override_sets_auto_approval_path() -> None:
    """Candidate auto-enqueued via source override gets approval_path='auto'."""
    cfg = _make_cfg(review_only=True)
    cfg.discovery_review_only_watchlist = False
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)

    enqueue_fn = MagicMock(return_value=(True, None, 1))
    with _patch_fetch([_good_candidate(MINT_A, source_id="watchlist")]):
        pipeline.run(state=state, enqueue_fn=enqueue_fn)

    rec = state.discovery_recent_candidates[0]
    assert rec.approval_path == "auto"


# ---------------------------------------------------------------------------
# 3. approve_discovery_candidate happy path
# ---------------------------------------------------------------------------

def test_approve_sets_provenance_fields() -> None:
    cfg = _make_cfg(review_only=True)
    state = _make_state()
    state.discovery_recent_candidates.append(_accepted_record(MINT_A, source_id="watchlist"))
    svc = SniperService(config=cfg, state=state)

    accepted, reason, _ = svc.approve_discovery_candidate(MINT_A, operator_id="alice")
    assert accepted is True
    assert reason is None

    rec = state.discovery_recent_candidates[0]
    assert rec.outcome == "enqueued"
    assert rec.approval_path == "operator_manual"
    assert rec.operator_approved_by == "alice"
    assert rec.operator_approved_at is not None
    assert rec.enqueue_source == "discovery_operator_approval"


def test_approve_bumps_total_enqueued_stat() -> None:
    cfg = _make_cfg(review_only=True)
    state = _make_state()
    state.discovery_recent_candidates.append(_accepted_record(MINT_A))
    svc = SniperService(config=cfg, state=state)

    svc.approve_discovery_candidate(MINT_A)
    assert state.discovery_stats.total_enqueued == 1


def test_approve_default_operator_id() -> None:
    """operator_id=None defaults to 'operator' in provenance."""
    cfg = _make_cfg(review_only=True)
    state = _make_state()
    state.discovery_recent_candidates.append(_accepted_record(MINT_A))
    svc = SniperService(config=cfg, state=state)

    svc.approve_discovery_candidate(MINT_A, operator_id=None)
    rec = state.discovery_recent_candidates[0]
    assert rec.operator_approved_by == "operator"


# ---------------------------------------------------------------------------
# 4. Duplicate approval blocked
# ---------------------------------------------------------------------------

def test_approve_blocks_double_approval() -> None:
    cfg = _make_cfg(review_only=True)
    state = _make_state()
    state.discovery_recent_candidates.append(_accepted_record(MINT_A))
    svc = SniperService(config=cfg, state=state)

    # First approval succeeds
    ok1, _, _ = svc.approve_discovery_candidate(MINT_A)
    assert ok1 is True

    # Second approval blocked: record now has outcome=enqueued
    ok2, reason2, _ = svc.approve_discovery_candidate(MINT_A)
    assert ok2 is False
    assert reason2 == "already_enqueued"


# ---------------------------------------------------------------------------
# 5. Missing mint blocked
# ---------------------------------------------------------------------------

def test_approve_missing_mint_returns_not_found() -> None:
    cfg = _make_cfg(review_only=True)
    state = _make_state()
    svc = SniperService(config=cfg, state=state)

    ok, reason, _ = svc.approve_discovery_candidate("Z" * 44)
    assert ok is False
    assert reason == "not_found_or_not_accepted"


def test_approve_empty_mint_returns_invalid() -> None:
    cfg = _make_cfg(review_only=True)
    state = _make_state()
    svc = SniperService(config=cfg, state=state)

    ok, reason, _ = svc.approve_discovery_candidate("")
    assert ok is False
    assert reason == "invalid_mint"


# ---------------------------------------------------------------------------
# 6. Already-enqueued record → already_enqueued
# ---------------------------------------------------------------------------

def test_approve_already_enqueued_outcome_is_blocked() -> None:
    cfg = _make_cfg(review_only=True)
    state = _make_state()
    rec = _accepted_record(MINT_A)
    rec.outcome = "enqueued"
    state.discovery_recent_candidates.append(rec)
    svc = SniperService(config=cfg, state=state)

    ok, reason, _ = svc.approve_discovery_candidate(MINT_A)
    assert ok is False
    assert reason == "already_enqueued"


# ---------------------------------------------------------------------------
# 7. Global review_only=True default preserved
# ---------------------------------------------------------------------------

def test_global_review_only_true_remains_default() -> None:
    """No env changes: discovery_review_only defaults to True in Config."""
    cfg = Config()
    assert cfg.discovery_review_only is True
    assert cfg.discovery_review_only_watchlist is None
    assert cfg.discovery_review_only_pumpfun is None
    assert cfg.discovery_review_only_whale_copy is None
    assert cfg.discovery_review_only_momentum is None


# ---------------------------------------------------------------------------
# 8. Dashboard review_only_overrides populated from env
# ---------------------------------------------------------------------------

def test_dashboard_review_only_overrides_from_env(monkeypatch) -> None:
    monkeypatch.setenv("DISCOVERY_REVIEW_ONLY_WATCHLIST", "false")
    monkeypatch.setenv("DISCOVERY_REVIEW_ONLY_WHALE_COPY", "true")
    monkeypatch.delenv("DISCOVERY_REVIEW_ONLY_PUMPFUN", raising=False)
    monkeypatch.delenv("DISCOVERY_REVIEW_ONLY_MOMENTUM", raising=False)

    from mint_ladder_bot.dashboard_server import _build_discovery_section
    result = _build_discovery_section(None)

    assert "review_only_overrides" in result
    overrides = result["review_only_overrides"]
    assert overrides.get("watchlist") is False
    assert overrides.get("whale_copy") is True
    # Not set → not in dict
    assert "pumpfun" not in overrides
    assert "momentum" not in overrides


def test_dashboard_review_only_overrides_empty_when_no_env(monkeypatch) -> None:
    for key in ("DISCOVERY_REVIEW_ONLY_WATCHLIST", "DISCOVERY_REVIEW_ONLY_PUMPFUN",
                "DISCOVERY_REVIEW_ONLY_WHALE_COPY", "DISCOVERY_REVIEW_ONLY_MOMENTUM"):
        monkeypatch.delenv(key, raising=False)

    from mint_ladder_bot.dashboard_server import _build_discovery_section
    result = _build_discovery_section(None)
    assert result["review_only_overrides"] == {}
