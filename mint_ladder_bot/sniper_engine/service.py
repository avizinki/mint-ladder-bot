from __future__ import annotations

from dataclasses import dataclass
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

        # Only pass enqueue_fn when gating allows live enqueue.
        review_only = getattr(self.config, "discovery_review_only", True)
        enqueue_fn = None
        if not review_only and self.mode() in ("live", "paper"):
            enqueue_fn = self.enqueue_manual_seed

        pipeline.run(state=self.state, enqueue_fn=enqueue_fn)

