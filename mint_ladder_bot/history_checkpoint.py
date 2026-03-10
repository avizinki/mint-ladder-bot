from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence


@dataclass
class HistoryCheckpoint:
    """
    Deterministic checkpoint for deep wallet-history scans.

    Semantics:
    - earliest_signature: the oldest (lowest-slot) signature that has been
      fully covered by scans so far.
    - earliest_slot: the slot of earliest_signature.
    - exhausted: True when history has been fully scanned (no more pages).

    Checkpoints are monotonic: earliest_slot can only move backward in time
    (to a smaller slot), and exhausted never flips back to False.
    """

    earliest_signature: Optional[str] = None
    earliest_slot: Optional[int] = None
    exhausted: bool = False
    updated_at: Optional[datetime] = None


@dataclass
class HistoryPageEntry:
    """
    Simplified representation of a history entry for checkpointing logic.

    Only the signature and slot ordering matter here; the full transaction
    body is handled elsewhere.
    """

    signature: str
    slot: int


def init_checkpoint_from_page(page: Sequence[HistoryPageEntry]) -> HistoryCheckpoint:
    """
    Initialize a checkpoint from the first fetched page.

    - If the page is empty: checkpoint is exhausted=True with no signature/slot.
    - Otherwise: earliest_* set to the entry with the smallest slot in the page.
    """
    if not page:
        return HistoryCheckpoint(earliest_signature=None, earliest_slot=None, exhausted=True, updated_at=None)
    oldest = min(page, key=lambda e: e.slot)
    return HistoryCheckpoint(
        earliest_signature=oldest.signature,
        earliest_slot=oldest.slot,
        exhausted=False,
        updated_at=None,
    )


def advance_checkpoint(
    checkpoint: HistoryCheckpoint,
    page: Sequence[HistoryPageEntry],
) -> HistoryCheckpoint:
    """
    Advance an existing checkpoint using a newly fetched older page.

    Rules:
    - Empty page:
      - Marks checkpoint.exhausted=True (no more history).
      - earliest_signature/earliest_slot remain unchanged.
    - Non-empty page when checkpoint has no earliest_slot yet:
      - Behaves like init_checkpoint_from_page.
    - Non-empty page when checkpoint has earliest_slot:
      - Let new_min be the entry with the smallest slot in the page.
      - If new_min.slot < earliest_slot: move checkpoint back to new_min.
      - If new_min.slot == earliest_slot:
          - If new_min.signature == earliest_signature: idempotent; no change.
          - Else: raise ValueError (inconsistent page ordering).
      - If new_min.slot > earliest_slot: raise ValueError (non-monotonic
        progression; page is newer than what has already been scanned).
    - exhausted never flips back to False.
    """
    if not page:
        # No more history; mark exhausted but keep existing earliest_*.
        if checkpoint.exhausted:
            return checkpoint
        return HistoryCheckpoint(
            earliest_signature=checkpoint.earliest_signature,
            earliest_slot=checkpoint.earliest_slot,
            exhausted=True,
            updated_at=checkpoint.updated_at,
        )

    if checkpoint.earliest_slot is None:
        return init_checkpoint_from_page(page)

    # Compute the oldest entry in the new page.
    oldest = min(page, key=lambda e: e.slot)
    new_sig, new_slot = oldest.signature, oldest.slot
    cur_sig, cur_slot = checkpoint.earliest_signature, checkpoint.earliest_slot

    if cur_slot is None:
        return init_checkpoint_from_page(page)

    # New page is strictly older than current checkpoint: move backward.
    if new_slot < cur_slot:
        return HistoryCheckpoint(
            earliest_signature=new_sig,
            earliest_slot=new_slot,
            exhausted=checkpoint.exhausted,
            updated_at=checkpoint.updated_at,
        )

    # Same slot as current checkpoint.
    if new_slot == cur_slot:
        if new_sig == cur_sig:
            # Idempotent re-processing of the same page; no change.
            return checkpoint
        # Same slot but different earliest signature: inconsistent ordering.
        raise ValueError(
            f"Non-deterministic page ordering: existing ({cur_sig}, {cur_slot}) vs new ({new_sig}, {new_slot})"
        )

    # new_slot > cur_slot means this page is newer than what we already scanned.
    raise ValueError(
        f"Non-monotonic checkpoint progression: new_min.slot={new_slot} > earliest_slot={cur_slot}"
    )


def next_before_anchor(checkpoint: HistoryCheckpoint) -> Optional[str]:
    """
    Compute the 'before' anchor (signature) for the next paginated request.

    - If exhausted: returns None (no further history expected).
    - Otherwise: returns earliest_signature, which should be passed as the
      'before' parameter for the next get_signatures_* call.
    """
    if checkpoint.exhausted:
        return None
    return checkpoint.earliest_signature

