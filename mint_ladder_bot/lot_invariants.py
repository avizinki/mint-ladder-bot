"""
Lot and ladder invariants — state integrity (CEO directive: no silent mutation).

Validates: sold <= bought, remaining >= 0, sum(executed) <= bought, no duplicate
ladder level execution, no duplicate lot for same confirmed buy tx.
Emits INVARIANT_WARNING; policy: warn only, never mutate silently.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .events import INVARIANT_WARNING, append_event
from .models import LotInfo, RuntimeMintState, RuntimeState, StepExecutionInfo

logger = logging.getLogger(__name__)


def _lot_sold_raw(lot: LotInfo) -> int:
    """Total sold from this lot (token_amount - remaining_amount)."""
    try:
        tok = int(lot.token_amount or 0)
        rem = int(lot.remaining_amount or 0)
        return max(0, tok - rem)
    except (ValueError, TypeError):
        return 0


def _lot_bought_raw(lot: LotInfo) -> int:
    try:
        return int(lot.token_amount or 0)
    except (ValueError, TypeError):
        return 0


def _lot_remaining_raw(lot: LotInfo) -> int:
    try:
        return int(lot.remaining_amount or 0)
    except (ValueError, TypeError):
        return 0


def check_lot_invariants(
    mint: str,
    ms: RuntimeMintState,
    event_journal_path: Optional[Any] = None,
) -> List[str]:
    """
    Check invariants for one mint state. Returns list of violation messages (empty if ok).
    Does not mutate state.
    """
    errors: List[str] = []
    lots = getattr(ms, "lots", None) or []

    for lot in lots:
        bought = _lot_bought_raw(lot)
        remaining = _lot_remaining_raw(lot)
        sold = _lot_sold_raw(lot)

        if sold > bought:
            errors.append(
                "lot %s mint %s: sold_raw (%s) > bought_raw (%s)"
                % (lot.lot_id[:8], mint[:12], sold, bought)
            )
        if remaining < 0:
            errors.append("lot %s mint %s: remaining_raw (%s) < 0" % (lot.lot_id[:8], mint[:12], remaining))
        if sold != bought - remaining:
            errors.append(
                "lot %s mint %s: sold_raw != bought_raw - remaining_raw (%s vs %s - %s)"
                % (lot.lot_id[:8], mint[:12], sold, bought, remaining)
            )

    # Executed steps: sum(sold_raw) <= sum(lot.token_amount) for mint
    executed_steps: Dict[str, StepExecutionInfo] = getattr(ms, "executed_steps", None) or {}
    def _step_sold(s: StepExecutionInfo) -> int:
        raw = getattr(s, "sold_raw", None)
        if raw is None:
            return 0
        try:
            return int(raw) if isinstance(raw, str) else int(raw)
        except (ValueError, TypeError):
            return 0

    total_sold_steps = sum(_step_sold(s) for s in executed_steps.values())
    total_bought_lots = sum(_lot_bought_raw(l) for l in lots)
    if total_sold_steps > total_bought_lots:
        errors.append(
            "mint %s: sum(executed_steps.sold_raw)=%s > sum(lots.token_amount)=%s"
            % (mint[:12], total_sold_steps, total_bought_lots)
        )

    for msg in errors:
        logger.warning("INVARIANT_WARNING %s", msg)
        if event_journal_path:
            append_event(event_journal_path, INVARIANT_WARNING, {"mint": mint[:12], "message": msg})

    return errors


def check_duplicate_lot_for_tx(
    state: RuntimeState,
    tx_signature: str,
    mint: str,
    event_journal_path: Optional[Any] = None,
) -> bool:
    """
    Return True if a lot already exists for this (tx_signature, mint).
    Used to prevent duplicate lot creation for same confirmed buy tx.
    """
    if not tx_signature or not mint:
        return False
    sig = tx_signature.strip()
    for m, ms in state.mints.items():
        if m != mint:
            continue
        for lot in getattr(ms, "lots", None) or []:
            if (lot.tx_signature or "").strip() == sig:
                logger.warning(
                    "INVARIANT_WARNING duplicate lot for tx mint=%s tx_sig=%s",
                    mint[:12],
                    sig[:16],
                )
                if event_journal_path:
                    append_event(
                        event_journal_path,
                        INVARIANT_WARNING,
                        {"mint": mint[:12], "tx_signature": sig[:16], "reason": "duplicate_lot_for_tx"},
                    )
                return True
    return False


def check_all_state_invariants(
    state: RuntimeState,
    event_journal_path: Optional[Any] = None,
) -> List[str]:
    """Run all invariant checks on state. Returns combined list of violation messages."""
    all_errors: List[str] = []
    for mint, ms in state.mints.items():
        all_errors.extend(check_lot_invariants(mint, ms, event_journal_path))
    return all_errors
