"""
Discovery pipeline.

Single entry point: DiscoveryPipeline.run()

Flow:
  fetch from sources → deduplicate → filter (token_filter) → score
  → gate (review_only / sniper_mode) → record to state → optionally enqueue

Rules:
- discovery_enabled=False → immediate no-op (state unchanged)
- duplicate mint (already in recent history / queue / open lot) → rejected
- token_filter rejection → rejected with stable reason code
- score below threshold → rejected with "score_blocked"
- discovery_review_only=True (default) → accepted candidates recorded but NOT enqueued
- discovery_review_only=False + sniper_mode=live → may enqueue
- live execution always requires explicit double-gate (review_only=False AND sniper_mode=live)

No changes are made to runner logic. Pipeline is called from SniperService.process_candidate_queue().
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ...config import Config
from ...models import DiscoveredCandidateRecord, DiscoveryStats, RuntimeState
from ..runtime import queue_contains_mint, pending_attempt_exists_for_mint, open_lot_exists_for_mint
from ..token_filter import filter_candidate, FilterResult, REASON_OK
from .model import DiscoveredCandidate
from .registry import fetch_from_sources
from .scoring import score_candidate, passes_score_threshold, DEFAULT_MIN_SCORE

logger = logging.getLogger(__name__)

# Rejection reason codes (stable; used in dashboard breakdown)
REASON_DISCOVERY_DISABLED = "discovery_disabled"
REASON_DUPLICATE_RECENT = "duplicate_recent_history"
REASON_ALREADY_QUEUED = "already_queued"
REASON_PENDING_ATTEMPT = "pending_attempt"
REASON_OPEN_LOT = "open_lot_exists"
REASON_SCORE_BLOCKED = "score_blocked"
REASON_REVIEW_ONLY = "review_only"  # accepted but not enqueued — not a real rejection


class DiscoveryPipeline:
    """
    Stateless pipeline object. State and config injected per call.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        state: RuntimeState,
        enqueue_fn: Optional[Any] = None,  # SniperService.enqueue_manual_seed callable, or None
    ) -> List[DiscoveredCandidateRecord]:
        """
        Run one discovery cycle.

        Returns list of DiscoveredCandidateRecord created this cycle (accepted + rejected).
        State (discovery_recent_candidates, discovery_rejected_candidates, discovery_stats) is
        mutated in place. Caller is responsible for saving state after.

        enqueue_fn: callable(mint, note) -> (accepted, reason, size) from SniperService.
          Passed only when review_only=False and sniper_mode != disabled.
          None = review_only mode (state records only, no enqueue).
        """
        if not getattr(self.config, "discovery_enabled", False):
            return []

        max_per_cycle = getattr(self.config, "discovery_max_candidates_per_cycle", 5)
        source_allowlist = getattr(self.config, "discovery_source_allowlist", None) or None
        min_score = getattr(self.config, "discovery_min_score", DEFAULT_MIN_SCORE)
        min_liq_usd = getattr(self.config, "sniper_min_liquidity_sol_equiv", 5_000.0)

        # Fetch raw candidates from all enabled sources
        raw_candidates = fetch_from_sources(
            source_allowlist=source_allowlist,
            limit_per_source=max(max_per_cycle, 20),
        )
        if not raw_candidates:
            return []

        created: List[DiscoveredCandidateRecord] = []
        seen_this_cycle: set = set()
        processed = 0

        for candidate in raw_candidates:
            if processed >= max_per_cycle:
                break

            # Deduplicate within this cycle
            if candidate.mint in seen_this_cycle:
                continue
            seen_this_cycle.add(candidate.mint)

            record, should_enqueue = self._process_one(candidate, state, min_score, min_liq_usd)

            if should_enqueue and enqueue_fn is not None:
                note = f"discovery:{candidate.source_id}"
                accepted, reason, _ = enqueue_fn(candidate.mint, note=note)
                if accepted:
                    record.outcome = "enqueued"
                    state.discovery_stats.total_enqueued += 1
                    logger.info(
                        "DISCOVERY_ENQUEUED mint=%s source=%s score=%.2f",
                        candidate.mint[:12], candidate.source_id,
                        candidate.score or 0.0,
                    )
                else:
                    # Enqueue rejected downstream (duplicate / cooldown / etc.)
                    record.outcome = "rejected"
                    record.rejection_reason = reason or "enqueue_rejected"
                    _bump_rejection_stat(state.discovery_stats, record.rejection_reason)
                    logger.debug(
                        "DISCOVERY_ENQUEUE_REJECTED mint=%s reason=%s",
                        candidate.mint[:12], reason,
                    )

            # Record to state AFTER final outcome is known (enqueue step may flip outcome)
            created.append(record)
            self._record_to_state(state, record)

            processed += 1

        logger.info(
            "DISCOVERY_CYCLE candidates_raw=%d processed=%d accepted=%d rejected=%d enqueued=%d",
            len(raw_candidates),
            processed,
            state.discovery_stats.total_accepted,
            state.discovery_stats.total_rejected,
            state.discovery_stats.total_enqueued,
        )
        return created

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_one(
        self,
        candidate: DiscoveredCandidate,
        state: RuntimeState,
        min_score: float,
        min_liq_usd: float,
    ) -> Tuple[DiscoveredCandidateRecord, bool]:
        """
        Evaluate one candidate. Returns (record, should_enqueue).

        should_enqueue is True only when candidate passes ALL gates AND review_only=False.
        """
        now = datetime.now(tz=timezone.utc)

        # --- Build base record ---
        record = DiscoveredCandidateRecord(
            record_id=str(uuid.uuid4()),
            mint=candidate.mint,
            source_id=candidate.source_id,
            source_confidence=candidate.source_confidence,
            discovered_at=candidate.discovered_at,
            symbol=candidate.symbol,
            liquidity_usd=candidate.liquidity_usd,
            deployer=candidate.deployer,
            metadata_blob=dict(candidate.metadata),
            score=None,
            outcome="pending",
            rejection_reason=None,
            processed_at=now,
        )

        # --- Duplicate / state gates ---
        rejection = self._check_state_gates(candidate.mint, state)
        if rejection:
            record.outcome = "rejected"
            record.rejection_reason = rejection
            _bump_rejection_stat(state.discovery_stats, rejection)
            state.discovery_stats.total_rejected += 1
            state.discovery_stats.total_discovered += 1
            _bump_source_stat(state.discovery_stats, candidate.source_id)
            logger.debug("DISCOVERY_REJECTED mint=%s reason=%s", candidate.mint[:12], rejection)
            return record, False

        # --- Token filter ---
        from ..launch_detector import LaunchCandidate
        lc = LaunchCandidate(
            mint=candidate.mint,
            source=candidate.source_id,
            detected_at=candidate.discovered_at,
            metadata={
                "symbol": candidate.symbol,
                "name": candidate.metadata.get("name"),
                "liquidity_usd": candidate.liquidity_usd,
                "deployer": candidate.deployer,
                **candidate.metadata,
            },
        )
        filter_result: FilterResult = filter_candidate(
            lc,
            min_liquidity_usd=min_liq_usd,
            require_metadata=False,  # metadata optional at discovery; scorer penalizes
        )
        if not filter_result.passed:
            record.outcome = "rejected"
            record.rejection_reason = filter_result.reason
            _bump_rejection_stat(state.discovery_stats, filter_result.reason)
            state.discovery_stats.total_rejected += 1
            state.discovery_stats.total_discovered += 1
            _bump_source_stat(state.discovery_stats, candidate.source_id)
            logger.debug(
                "DISCOVERY_FILTER_REJECTED mint=%s reason=%s",
                candidate.mint[:12], filter_result.reason,
            )
            return record, False

        # --- Score ---
        score = score_candidate(candidate, min_liquidity_usd=min_liq_usd)
        candidate.score = score
        record.score = score

        if not passes_score_threshold(score, min_score):
            record.outcome = "rejected"
            record.rejection_reason = REASON_SCORE_BLOCKED
            _bump_rejection_stat(state.discovery_stats, REASON_SCORE_BLOCKED)
            state.discovery_stats.total_rejected += 1
            state.discovery_stats.total_discovered += 1
            _bump_source_stat(state.discovery_stats, candidate.source_id)
            logger.debug(
                "DISCOVERY_SCORE_BLOCKED mint=%s score=%.2f min=%.2f",
                candidate.mint[:12], score, min_score,
            )
            return record, False

        # --- Accepted ---
        record.outcome = "accepted"
        state.discovery_stats.total_accepted += 1
        state.discovery_stats.total_discovered += 1
        _bump_source_stat(state.discovery_stats, candidate.source_id)

        review_only = getattr(self.config, "discovery_review_only", True)
        if review_only:
            logger.info(
                "DISCOVERY_ACCEPTED_REVIEW_ONLY mint=%s source=%s score=%.2f",
                candidate.mint[:12], candidate.source_id, score,
            )
            return record, False

        # Should enqueue — caller decides actual enqueue based on sniper_mode
        logger.info(
            "DISCOVERY_ACCEPTED mint=%s source=%s score=%.2f",
            candidate.mint[:12], candidate.source_id, score,
        )
        return record, True

    def _check_state_gates(self, mint: str, state: RuntimeState) -> Optional[str]:
        """Return rejection reason string if mint should be blocked, else None."""
        # Already in recent accepted history this session
        for rec in state.discovery_recent_candidates:
            if rec.mint == mint and rec.outcome in ("accepted", "enqueued"):
                return REASON_DUPLICATE_RECENT

        if queue_contains_mint(state, mint):
            return REASON_ALREADY_QUEUED
        if pending_attempt_exists_for_mint(state, mint):
            return REASON_PENDING_ATTEMPT
        if open_lot_exists_for_mint(state, mint):
            return REASON_OPEN_LOT
        return None

    def _record_to_state(self, state: RuntimeState, record: DiscoveredCandidateRecord) -> None:
        """Append record to appropriate bounded history list."""
        max_history = getattr(self.config, "discovery_max_history", 200)
        max_rejected = getattr(self.config, "discovery_max_rejected", 200)

        if record.outcome in ("rejected",):
            state.discovery_rejected_candidates.append(record)
            if len(state.discovery_rejected_candidates) > max_rejected:
                state.discovery_rejected_candidates = state.discovery_rejected_candidates[-max_rejected:]
        else:
            # pending / accepted / enqueued all go to recent list
            state.discovery_recent_candidates.append(record)
            if len(state.discovery_recent_candidates) > max_history:
                state.discovery_recent_candidates = state.discovery_recent_candidates[-max_history:]


# ------------------------------------------------------------------
# Stat helpers
# ------------------------------------------------------------------

def _bump_rejection_stat(stats: DiscoveryStats, reason: str) -> None:
    stats.by_rejection_reason[reason] = stats.by_rejection_reason.get(reason, 0) + 1


def _bump_source_stat(stats: DiscoveryStats, source_id: str) -> None:
    stats.by_source[source_id] = stats.by_source.get(source_id, 0) + 1
