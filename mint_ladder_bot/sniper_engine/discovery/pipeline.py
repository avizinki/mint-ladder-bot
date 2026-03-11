"""
Discovery pipeline.

Single entry point: DiscoveryPipeline.run()

Flow:
  fetch from sources → deduplicate → filter (token_filter) → enrich → score
  → gate (review_only / sniper_mode) → record to state → optionally enqueue

Rules:
- discovery_enabled=False → immediate no-op (state unchanged)
- duplicate mint (already in recent history / queue / open lot) → rejected
- token_filter rejection → rejected with stable reason code
- enrichment hard-block → rejected with enrichment reason code
- enrichment failure (partial) → candidate proceeds with score penalty, not rejected
- score below threshold → rejected with "score_blocked"
- discovery_review_only=True (default) → accepted candidates recorded but NOT enqueued
- discovery_review_only=False + sniper_mode=live → may enqueue
- live execution always requires explicit double-gate (review_only=False AND sniper_mode=live)
- metadata_blob is truncated to 2048 bytes at record creation (mitigation #4)

No changes are made to runner logic. Pipeline is called from SniperService.process_candidate_queue().
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ...config import Config
from ...models import DiscoveredCandidateRecord, DiscoveryStats, RuntimeState
from ..runtime import queue_contains_mint, pending_attempt_exists_for_mint, open_lot_exists_for_mint
from ..token_filter import filter_candidate, FilterResult, REASON_OK
from .enrichment import CandidateEnricher, EnrichmentResult, make_enricher_from_config
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

# metadata_blob max size in bytes (mitigation #4)
_METADATA_MAX_BYTES = 2048


class DiscoveryPipeline:
    """
    Stateless pipeline object. State and config injected per call.
    Enricher is constructed lazily and reused across calls.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._enricher: Optional[CandidateEnricher] = None

    def _get_enricher(self) -> CandidateEnricher:
        if self._enricher is None:
            self._enricher = make_enricher_from_config(self.config)
        return self._enricher

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

        # Per-cycle enrichment cache — cleared each run()
        cycle_cache: Dict[str, EnrichmentResult] = {}

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

            record, should_enqueue = self._process_one(
                candidate, state, min_score, min_liq_usd, cycle_cache
            )

            if should_enqueue and enqueue_fn is not None:
                note = f"discovery:{candidate.source_id}"
                accepted, reason, _ = enqueue_fn(candidate.mint, note=note)
                if accepted:
                    record.outcome = "enqueued"
                    record.enqueue_source = "discovery_auto"
                    record.approval_path = "auto"
                    state.discovery_stats.total_enqueued += 1
                    _bump_source_sub_stat(state.discovery_stats, candidate.source_id, "enqueued")
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
        cycle_cache: Optional[Dict[str, EnrichmentResult]] = None,
    ) -> Tuple[DiscoveredCandidateRecord, bool]:
        """
        Evaluate one candidate. Returns (record, should_enqueue).

        should_enqueue is True only when candidate passes ALL gates AND review_only=False.
        """
        now = datetime.now(tz=timezone.utc)

        if cycle_cache is None:
            cycle_cache = {}

        # --- Build base record with truncated metadata_blob (mitigation #4) ---
        raw_meta = dict(candidate.metadata)
        truncated, meta_blob = _truncate_metadata(raw_meta)
        if truncated:
            logger.warning(
                "DISCOVERY_METADATA_TRUNCATED mint=%s source=%s",
                candidate.mint[:12], candidate.source_id,
            )

        record = DiscoveredCandidateRecord(
            record_id=str(uuid.uuid4()),
            mint=candidate.mint,
            source_id=candidate.source_id,
            source_confidence=candidate.source_confidence,
            discovered_at=candidate.discovered_at,
            symbol=candidate.symbol,
            liquidity_usd=candidate.liquidity_usd,
            deployer=candidate.deployer,
            metadata_blob=meta_blob,
            metadata_truncated=truncated,
            score=None,
            score_breakdown={},
            outcome="pending",
            rejection_reason=None,
            processed_at=now,
            discovery_signals=dict(candidate.discovery_signals),
            enrichment_data={},
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
            _bump_source_sub_stat(state.discovery_stats, candidate.source_id, "rejected")
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
            _bump_source_sub_stat(state.discovery_stats, candidate.source_id, "rejected")
            logger.debug(
                "DISCOVERY_FILTER_REJECTED mint=%s reason=%s",
                candidate.mint[:12], filter_result.reason,
            )
            return record, False

        # --- Enrichment (mitigation #2: soft-failure) ---
        enricher = self._get_enricher()
        enrich_result = enricher.enrich(
            mint=candidate.mint,
            candidate_liquidity_usd=candidate.liquidity_usd,
            cycle_cache=cycle_cache,
        )
        record.enrichment_data = enrich_result.data

        # Track enrichment stats
        state.discovery_stats.enrichment_checks_run += 1
        if enrich_result.partial:
            state.discovery_stats.enrichment_partial_count += 1

        if enrich_result.hard_block:
            # Confirmed risk — hard-block
            state.discovery_stats.enrichment_hard_reject_count += 1
            record.outcome = "rejected"
            record.rejection_reason = enrich_result.rejection_reason or "enrichment_risk"
            _bump_rejection_stat(state.discovery_stats, record.rejection_reason)
            state.discovery_stats.total_rejected += 1
            state.discovery_stats.total_discovered += 1
            _bump_source_stat(state.discovery_stats, candidate.source_id)
            _bump_source_sub_stat(state.discovery_stats, candidate.source_id, "rejected")
            logger.info(
                "DISCOVERY_ENRICHMENT_REJECTED mint=%s reason=%s",
                candidate.mint[:12], record.rejection_reason,
            )
            return record, False

        # --- Score (with enrichment data for partial penalty) ---
        score, score_breakdown = score_candidate(
            candidate,
            min_liquidity_usd=min_liq_usd,
            enrichment_data=enrich_result.data if enrich_result.partial else None,
        )
        candidate.score = score
        record.score = score
        record.score_breakdown = score_breakdown

        if not passes_score_threshold(score, min_score):
            record.outcome = "rejected"
            record.rejection_reason = REASON_SCORE_BLOCKED
            _bump_rejection_stat(state.discovery_stats, REASON_SCORE_BLOCKED)
            state.discovery_stats.total_rejected += 1
            state.discovery_stats.total_discovered += 1
            _bump_source_stat(state.discovery_stats, candidate.source_id)
            _bump_source_sub_stat(state.discovery_stats, candidate.source_id, "rejected")
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
        _bump_source_sub_stat(state.discovery_stats, candidate.source_id, "accepted")

        review_only = _resolve_source_review_only(self.config, candidate.source_id)
        if review_only:
            logger.info(
                "DISCOVERY_ACCEPTED_REVIEW_ONLY mint=%s source=%s score=%.2f",
                candidate.mint[:12], candidate.source_id, score,
            )
            return record, False

        # Should enqueue — caller decides actual enqueue based on sniper_mode
        record.approval_path = "auto"
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
# Metadata truncation (mitigation #4)
# ------------------------------------------------------------------

def _truncate_metadata(meta: Dict) -> Tuple[bool, Dict]:
    """
    Return (was_truncated, safe_meta_dict).
    If JSON-serialized size > _METADATA_MAX_BYTES, returns a truncated copy.
    """
    try:
        serialized = json.dumps(meta, default=str)
    except Exception:
        return True, {}

    if len(serialized.encode("utf-8")) <= _METADATA_MAX_BYTES:
        return False, meta

    # Truncate: keep keys until we exceed limit
    truncated: Dict = {}
    running = 2  # account for "{}"
    for k, v in meta.items():
        try:
            entry = json.dumps({k: v}, default=str)
        except Exception:
            continue
        entry_bytes = len(entry.encode("utf-8")) - 2  # strip outer {}
        if running + entry_bytes > _METADATA_MAX_BYTES:
            break
        truncated[k] = v
        running += entry_bytes + 1  # +1 for comma
    return True, truncated


# ------------------------------------------------------------------
# Stat helpers
# ------------------------------------------------------------------

def _bump_rejection_stat(stats: DiscoveryStats, reason: str) -> None:
    stats.by_rejection_reason[reason] = stats.by_rejection_reason.get(reason, 0) + 1


def _bump_source_stat(stats: DiscoveryStats, source_id: str) -> None:
    stats.by_source[source_id] = stats.by_source.get(source_id, 0) + 1


def _bump_source_sub_stat(stats: DiscoveryStats, source_id: str, field: str) -> None:
    """Update per-source sub-stats: {source_id: {discovered, accepted, rejected, enqueued}}."""
    if source_id not in stats.source_stats:
        stats.source_stats[source_id] = {
            "discovered": 0,
            "accepted": 0,
            "rejected": 0,
            "enqueued": 0,
        }
    bucket = stats.source_stats[source_id]
    bucket[field] = bucket.get(field, 0) + 1


def _resolve_source_review_only(config: Any, source_id: str) -> bool:
    """
    Resolve effective review_only flag for a given source.

    Per-source override (DISCOVERY_REVIEW_ONLY_<SOURCE_ID>) takes precedence.
    Falls back to global discovery_review_only (default True).
    """
    global_ro: bool = getattr(config, "discovery_review_only", True)
    attr = f"discovery_review_only_{source_id.lower()}"
    override = getattr(config, attr, None)
    if override is None:
        return global_ro
    return override
