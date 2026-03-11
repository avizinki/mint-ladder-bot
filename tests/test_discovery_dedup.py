"""
Duplicate candidate suppression tests.

A mint must be rejected with a stable reason code when:
1. It already appears in discovery_recent_candidates (outcome=accepted/enqueued)
2. It is already in the sniper manual seed queue
3. It has a pending sniper attempt
4. It has an open lot in state
"""
from __future__ import annotations

from datetime import datetime, timezone

from mint_ladder_bot.config import Config
from mint_ladder_bot.models import (
    DiscoveredCandidateRecord,
    LotInfo,
    RuntimeMintState,
    RuntimeState,
    SniperAttempt,
    SniperManualSeedQueueEntry,
    SolBalance,
)
from mint_ladder_bot.sniper_engine.discovery.pipeline import (
    DiscoveryPipeline,
    REASON_DUPLICATE_RECENT,
    REASON_ALREADY_QUEUED,
    REASON_PENDING_ATTEMPT,
    REASON_OPEN_LOT,
)
from mint_ladder_bot.sniper_engine.discovery.model import DiscoveredCandidate

FAKE_MINT = "D" * 44


def _make_state() -> RuntimeState:
    return RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        wallet="WALLET_OK",
        sol=SolBalance(lamports=0, sol=0.0),
        mints={},
    )


def _make_cfg() -> Config:
    cfg = Config()
    cfg.discovery_enabled = True
    cfg.discovery_review_only = True
    cfg.discovery_max_candidates_per_cycle = 10
    cfg.sniper_min_score = 0.0  # accept any score for dedup tests
    cfg.sniper_min_liquidity_sol_equiv = 0.0
    return cfg


def _make_candidate(mint: str = FAKE_MINT) -> DiscoveredCandidate:
    return DiscoveredCandidate(
        mint=mint,
        source_id="test",
        source_confidence=1.0,
        discovered_at=datetime.now(tz=timezone.utc),
        symbol="DUP",
    )


# ---------------------------------------------------------------------------
# Test 1: Already in recent accepted history
# ---------------------------------------------------------------------------

def test_duplicate_in_recent_history_rejected() -> None:
    cfg = _make_cfg()
    state = _make_state()

    # Pre-populate recent_candidates with accepted record
    existing = DiscoveredCandidateRecord(
        record_id="existing-id",
        mint=FAKE_MINT,
        source_id="test",
        source_confidence=1.0,
        discovered_at=datetime.now(tz=timezone.utc),
        outcome="accepted",
    )
    state.discovery_recent_candidates.append(existing)

    pipeline = DiscoveryPipeline(cfg)
    candidate = _make_candidate()

    record, should_enqueue = pipeline._process_one(candidate, state, min_score=0.0, min_liq_usd=0.0)

    assert not should_enqueue
    assert record.outcome == "rejected"
    assert record.rejection_reason == REASON_DUPLICATE_RECENT


def test_duplicate_in_recent_pending_is_not_blocked() -> None:
    """outcome=pending in recent history should NOT block (only accepted/enqueued do)."""
    cfg = _make_cfg()
    state = _make_state()

    existing = DiscoveredCandidateRecord(
        record_id="pending-id",
        mint=FAKE_MINT,
        source_id="test",
        source_confidence=1.0,
        discovered_at=datetime.now(tz=timezone.utc),
        outcome="pending",  # pending does not block
    )
    state.discovery_recent_candidates.append(existing)

    pipeline = DiscoveryPipeline(cfg)
    candidate = _make_candidate()

    record, _ = pipeline._process_one(candidate, state, min_score=0.0, min_liq_usd=0.0)
    # pending in history does not trigger duplicate gate
    assert record.rejection_reason != REASON_DUPLICATE_RECENT


# ---------------------------------------------------------------------------
# Test 2: Already in manual seed queue
# ---------------------------------------------------------------------------

def test_already_in_queue_rejected() -> None:
    cfg = _make_cfg()
    state = _make_state()
    state.sniper_manual_seed_queue.append(
        SniperManualSeedQueueEntry(mint=FAKE_MINT, enqueued_at=0)
    )

    pipeline = DiscoveryPipeline(cfg)
    candidate = _make_candidate()
    record, should_enqueue = pipeline._process_one(candidate, state, min_score=0.0, min_liq_usd=0.0)

    assert not should_enqueue
    assert record.outcome == "rejected"
    assert record.rejection_reason == REASON_ALREADY_QUEUED


# ---------------------------------------------------------------------------
# Test 3: Pending sniper attempt
# ---------------------------------------------------------------------------

def test_pending_attempt_blocks_candidate() -> None:
    cfg = _make_cfg()
    state = _make_state()
    attempt = SniperAttempt(
        attempt_id="att-1",
        candidate_id="cand-1",
        mint=FAKE_MINT,
        discovery_source="test",
        state="submitted",
        created_at=0,
    )
    state.sniper_pending_attempts["att-1"] = attempt

    pipeline = DiscoveryPipeline(cfg)
    candidate = _make_candidate()
    record, should_enqueue = pipeline._process_one(candidate, state, min_score=0.0, min_liq_usd=0.0)

    assert not should_enqueue
    assert record.outcome == "rejected"
    assert record.rejection_reason == REASON_PENDING_ATTEMPT


# ---------------------------------------------------------------------------
# Test 4: Open lot exists
# ---------------------------------------------------------------------------

def test_open_lot_blocks_candidate() -> None:
    cfg = _make_cfg()
    state = _make_state()
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw="1000",
        moonbag_raw="0",
        lots=[LotInfo(mint=FAKE_MINT, token_amount="1000", remaining_amount="1000")],
    )
    state.mints[FAKE_MINT] = ms

    pipeline = DiscoveryPipeline(cfg)
    candidate = _make_candidate()
    record, should_enqueue = pipeline._process_one(candidate, state, min_score=0.0, min_liq_usd=0.0)

    assert not should_enqueue
    assert record.outcome == "rejected"
    assert record.rejection_reason == REASON_OPEN_LOT


# ---------------------------------------------------------------------------
# Test 5: Within-cycle dedup (same mint from two sources)
# ---------------------------------------------------------------------------

def test_within_cycle_dedup_via_seen_set() -> None:
    """Two candidates with same mint in one cycle — second must be silently skipped (seen_this_cycle set)."""
    cfg = _make_cfg()
    state = _make_state()

    mint_a = "A" * 44
    mint_b = "B" * 44

    candidates = [
        DiscoveredCandidate(mint=mint_a, source_id="test", source_confidence=1.0, discovered_at=datetime.now(tz=timezone.utc), symbol="A"),
        DiscoveredCandidate(mint=mint_a, source_id="pumpfun", source_confidence=0.5, discovered_at=datetime.now(tz=timezone.utc), symbol="A"),  # duplicate
        DiscoveredCandidate(mint=mint_b, source_id="test", source_confidence=1.0, discovered_at=datetime.now(tz=timezone.utc), symbol="B"),
    ]

    with _patch_fetch(candidates):
        pipeline = DiscoveryPipeline(cfg)
        pipeline.run(state=state)

    # mint_a should appear once; mint_b once
    all_mints = [r.mint for r in state.discovery_recent_candidates] + [r.mint for r in state.discovery_rejected_candidates]
    assert all_mints.count(mint_a) == 1
    assert all_mints.count(mint_b) == 1


def _patch_fetch(candidates):
    """Context manager: patch fetch_from_sources to return given list."""
    from unittest.mock import patch
    return patch(
        "mint_ladder_bot.sniper_engine.discovery.pipeline.fetch_from_sources",
        return_value=candidates,
    )
