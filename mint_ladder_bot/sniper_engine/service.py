from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import time
from typing import Optional, Tuple

from ..config import Config
from ..models import RuntimeState
from . import runtime as rt


EnqueueResult = Tuple[bool, Optional[str], int]


@dataclass
class SniperService:
    config: Config
    state: RuntimeState

    def is_enabled(self) -> bool:
        """True when sniper subsystem is allowed to run at all."""
        return bool(getattr(self.config, "sniper_enabled", False)) and self.mode() != "disabled"

    def mode(self) -> str:
        """Return current sniper mode: disabled | paper | live."""
        mode = (getattr(self.config, "sniper_mode", "disabled") or "disabled").lower()
        if mode not in ("disabled", "paper", "live"):
            return "disabled"
        return mode

    def is_live_mode(self) -> bool:
        return self.is_enabled() and self.mode() == "live"

    def is_paper_mode(self) -> bool:
        return self.is_enabled() and self.mode() == "paper"

    # ---- Queue operations ----

    def enqueue_manual_seed(self, mint: str, note: Optional[str] = None) -> EnqueueResult:
        """
        Enqueue a manual-seed mint into sniper_manual_seed_queue.

        Returns (accepted, reason, queue_size).
        """
        # Disabled: reject explicitly so operator intent is clear.
        if not self.is_enabled():
            return False, "disabled", len(self.state.sniper_manual_seed_queue)

        mint = (mint or "").strip()
        if not mint:
            return False, "invalid_mint", len(self.state.sniper_manual_seed_queue)

        max_size = getattr(self.config, "sniper_max_manual_queue_size", 100)
        now_ts = int(time())

        # Low-level helper enforces queue_full / duplicate / pending_attempt / open_lot.
        if len(self.state.sniper_manual_seed_queue) >= max_size:
            return False, "queue_full", len(self.state.sniper_manual_seed_queue)

        if rt.queue_contains_mint(self.state, mint):
            return False, "duplicate_in_queue", len(self.state.sniper_manual_seed_queue)

        if rt.pending_attempt_exists_for_mint(self.state, mint):
            return False, "pending_attempt_exists", len(self.state.sniper_manual_seed_queue)

        if rt.open_lot_exists_for_mint(self.state, mint):
            return False, "open_lot_exists", len(self.state.sniper_manual_seed_queue)

        # Additional mint-state based blocking can be layered later; for now, blocked_mint_state is reserved.
        accepted = rt.enqueue_manual_seed(
            state=self.state,
            mint=mint,
            enqueued_at=now_ts,
            note=note,
            max_queue_size=max_size,
        )
        if not accepted:
            # Fallback generic reason for any future helper guards.
            return False, "blocked_mint_state", len(self.state.sniper_manual_seed_queue)
        return True, None, len(self.state.sniper_manual_seed_queue)

    def approve_discovery_candidate(
        self,
        mint: str,
        operator_id: Optional[str] = None,
    ) -> EnqueueResult:
        """
        Operator-approve a discovery candidate that is in review_only accepted state.

        Validates that:
        - The mint has a record in discovery_recent_candidates with outcome="accepted"
        - The mint is not already enqueued (would also be caught by enqueue_manual_seed)

        On success:
        - Calls enqueue_manual_seed with note="discovery_operator_approval"
        - Updates the record's provenance fields: approval_path, operator_approved_at,
          operator_approved_by, enqueue_source, outcome
        - Bumps discovery_stats.total_enqueued

        Returns (accepted, reason, queue_size).
        """
        mint = (mint or "").strip()
        if not mint:
            return False, "invalid_mint", len(self.state.sniper_manual_seed_queue)

        # Find existing accepted discovery record
        accepted_record = None
        for rec in self.state.discovery_recent_candidates:
            if rec.mint == mint and rec.outcome == "accepted":
                accepted_record = rec
                break

        if accepted_record is None:
            # Also check if already enqueued (operator trying to double-approve)
            for rec in self.state.discovery_recent_candidates:
                if rec.mint == mint and rec.outcome == "enqueued":
                    return False, "already_enqueued", len(self.state.sniper_manual_seed_queue)
            return False, "not_found_or_not_accepted", len(self.state.sniper_manual_seed_queue)

        note = f"discovery_operator_approval:{accepted_record.source_id}"
        ok, reason, queue_size = self.enqueue_manual_seed(mint, note=note)
        if not ok:
            return False, reason, queue_size

        # Update provenance fields on the existing record
        now = datetime.now(tz=timezone.utc)
        accepted_record.outcome = "enqueued"
        accepted_record.approval_path = "operator_manual"
        accepted_record.operator_approved_at = now
        accepted_record.operator_approved_by = operator_id or "operator"
        accepted_record.enqueue_source = "discovery_operator_approval"
        self.state.discovery_stats.total_enqueued += 1
        from .discovery.pipeline import _bump_source_sub_stat
        _bump_source_sub_stat(self.state.discovery_stats, accepted_record.source_id, "enqueued")

        return True, None, queue_size

    def dequeue_next_manual_seed_batch(self, limit: int):
        """Thin wrapper around runtime helper for future processing; currently unused when disabled."""
        if not self.is_enabled():
            return []
        return rt.dequeue_next_manual_seed_batch(self.state, limit)

    # ---- Cycle hooks (placeholders for now) ----

    def resolve_pending_attempts(self) -> None:
        """Placeholder: in Phase 1 this will reconcile pending attempts; currently no-op."""
        if not self.is_enabled():
            return
        # No logic yet; future reconciliation will live here.
        return

    def process_candidate_queue(self) -> None:
        """
        Run the discovery pipeline if discovery is enabled.

        Collects candidates from registered sources, normalizes, filters, scores,
        and (when review_only=False and mode=live) enqueues accepted candidates.

        Safety gating:
        - discovery_enabled=False → no-op (default)
        - discovery_review_only=True (default) → candidates recorded, never enqueued
        - sniper disabled → never enqueues regardless of review_only setting
        """
        if not self.is_enabled():
            return
        if not getattr(self.config, "discovery_enabled", False):
            return

        # Lazy-register sources once per process.
        try:
            from .discovery.sources import register_all
            register_all()
        except Exception:
            pass

        from .discovery.pipeline import DiscoveryPipeline

        pipeline = DiscoveryPipeline(config=self.config)

        # Pass enqueue_fn when any source has auto-enqueue enabled (review_only=False).
        # Per-source overrides can unlock individual sources while global remains advisory.
        global_review_only = getattr(self.config, "discovery_review_only", True)
        per_source_overrides = [
            getattr(self.config, "discovery_review_only_watchlist", None),
            getattr(self.config, "discovery_review_only_pumpfun", None),
            getattr(self.config, "discovery_review_only_whale_copy", None),
            getattr(self.config, "discovery_review_only_momentum", None),
        ]
        any_auto_enqueue = not global_review_only or any(v is False for v in per_source_overrides)

        enqueue_fn = None
        if any_auto_enqueue and self.mode() in ("live", "paper"):
            enqueue_fn = self.enqueue_manual_seed

        pipeline.run(state=self.state, enqueue_fn=enqueue_fn)

