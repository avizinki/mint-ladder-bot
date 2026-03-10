from __future__ import annotations

from typing import Dict, List, Tuple

from ..models import (
    RuntimeState,
    RuntimeMintState,
    SniperAttempt,
    SniperAttemptState,
    SniperManualSeedQueueEntry,
)


PENDING_STATES: Tuple[SniperAttemptState, ...] = (
    "created",
    "quoted",
    "submitted",
    "pending_chain_observation",
    "observed_candidate_receipt",
)

TERMINAL_STATES: Tuple[SniperAttemptState, ...] = (
    "quote_rejected",
    "resolved_success",
    "resolved_failed",
    "resolved_uncertain",
)


def is_pending_sniper_attempt_state(state: SniperAttemptState) -> bool:
    return state in PENDING_STATES


def is_terminal_sniper_attempt_state(state: SniperAttemptState) -> bool:
    return state in TERMINAL_STATES


def queue_contains_mint(state: RuntimeState, mint: str) -> bool:
    return any(entry.mint == mint for entry in state.sniper_manual_seed_queue)


def pending_attempt_exists_for_mint(state: RuntimeState, mint: str) -> bool:
    for attempt in state.sniper_pending_attempts.values():
        if attempt.mint == mint and is_pending_sniper_attempt_state(attempt.state):
            return True
    return False


def open_lot_exists_for_mint(state: RuntimeState, mint: str) -> bool:
    mint_state: RuntimeMintState | None = state.mints.get(mint)
    if mint_state is None:
        return False
    for lot in mint_state.lots or []:
        try:
            remaining = int(getattr(lot, "remaining_amount", "0") or "0")
        except (TypeError, ValueError):
            remaining = 0
        if remaining > 0:
            return True
    return False


def mint_is_blocked_for_enqueue(state: RuntimeState, mint: str) -> bool:
    if queue_contains_mint(state, mint):
        return True
    if pending_attempt_exists_for_mint(state, mint):
        return True
    if open_lot_exists_for_mint(state, mint):
        return True
    # Quarantine / non-tradable checks will be layered on top in the service; keep this helper narrowly focused.
    return False


def enqueue_manual_seed(state: RuntimeState, mint: str, enqueued_at: int, note: str | None, max_queue_size: int) -> bool:
    if len(state.sniper_manual_seed_queue) >= max_queue_size:
        return False
    if mint_is_blocked_for_enqueue(state, mint):
        return False
    entry = SniperManualSeedQueueEntry(mint=mint, enqueued_at=enqueued_at, note=note)
    state.sniper_manual_seed_queue.append(entry)
    return True


def dequeue_next_manual_seed_batch(state: RuntimeState, limit: int) -> List[SniperManualSeedQueueEntry]:
    if limit <= 0:
        return []
    batch = state.sniper_manual_seed_queue[:limit]
    state.sniper_manual_seed_queue = state.sniper_manual_seed_queue[limit:]
    return batch


def add_pending_attempt(state: RuntimeState, attempt: SniperAttempt) -> None:
    if is_terminal_sniper_attempt_state(attempt.state):
        raise ValueError(f"Cannot add terminal attempt state to pending: {attempt.state}")
    state.sniper_pending_attempts[attempt.attempt_id] = attempt


def move_attempt_to_history(state: RuntimeState, attempt_id: str, new_state: SniperAttemptState) -> None:
    attempt = state.sniper_pending_attempts.pop(attempt_id, None)
    if attempt is None:
        return
    attempt.state = new_state
    state.sniper_attempt_history.append(attempt)


def remove_pending_attempt(state: RuntimeState, attempt_id: str) -> None:
    state.sniper_pending_attempts.pop(attempt_id, None)

