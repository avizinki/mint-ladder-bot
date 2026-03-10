"""
Integration: create lot in mint-ladder-bot state format so ladder engine arms TP levels.

When a sniper buy fills, ensure mint exists in state, append LotInfo, update trading_bag_raw, save state.
Optional event journal for observability (LAUNCH_DETECTED, BUY_CONFIRMED, LOT_CREATED).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# Use mint_ladder_bot state and models for compatibility.
from ..events import (
    BUY_CONFIRMED,
    append_event,
    EVENT_LOT_CREATED,
)
from ..models import LotInfo, RuntimeMintState, RuntimeState
from ..state import load_state, save_state_atomic


def ensure_mint_in_state(
    state: RuntimeState,
    mint: str,
    trading_bag_raw: str,
    moonbag_raw: str = "0",
    entry_price_sol_per_token: float = 0.0,
) -> RuntimeMintState:
    """
    Ensure state.mints[mint] exists with required fields (lots, executed_steps, failures, buybacks).
    If mint is new, create RuntimeMintState; otherwise return existing.
    """
    from ..state import ensure_mint_state as _ensure_mint_state

    _ensure_mint_state(
        state=state,
        mint=mint,
        entry_price_sol_per_token=entry_price_sol_per_token,
        trading_bag_raw=int(trading_bag_raw),
        moonbag_raw=int(moonbag_raw),
        entry_source=None,
    )
    return state.mints[mint]


def create_lot_in_state(
    state: RuntimeState,
    mint: str,
    token_amount_raw: int,
    entry_price_sol_per_token: float,
    tx_signature: str,
    trading_bag_raw: str,
    moonbag_raw: str = "0",
    program_or_venue: str = "jupiter",
) -> LotInfo:
    """
    Create a LotInfo compatible with mint-ladder-bot and append to state.mints[mint].lots.
    Ensures mint exists, then appends lot with source=tx_exact, swap_type=sol_to_token.
    Caller should call save_state_atomic(state_path, state) after.
    """
    ms = ensure_mint_in_state(
        state,
        mint,
        trading_bag_raw=trading_bag_raw,
        moonbag_raw=moonbag_raw,
        entry_price_sol_per_token=entry_price_sol_per_token,
    )
    lot = LotInfo.create(
        mint=mint,
        token_amount_raw=token_amount_raw,
        entry_price=entry_price_sol_per_token,
        confidence="known",
        source="tx_exact",
        entry_confidence="exact",
        tx_signature=tx_signature,
        swap_type="sol_to_token",
        input_asset_mint=None,
        input_amount_raw=None,
        program_or_venue=program_or_venue,
        acquired_via_swap=False,
        valuation_method="sol_spent",
    )
    ms.lots = getattr(ms, "lots", None) or []
    ms.lots.append(lot)
    # Keep trading_bag in sync (e.g. sum of lot remaining_amount for sellable)
    ms.trading_bag_raw = trading_bag_raw
    ms.entry_price_sol_per_token = entry_price_sol_per_token
    ms.working_entry_price_sol_per_token = entry_price_sol_per_token
    if getattr(ms, "original_entry_price_sol_per_token", None) is None:
        ms.original_entry_price_sol_per_token = entry_price_sol_per_token
    return lot


def persist_sniper_lot(
    state_path: Path,
    status_path: Path,
    state: RuntimeState,
    journal_path: Optional[Path] = None,
    created_lot: Optional[LotInfo] = None,
    token_raw: Optional[int] = None,
    buy_sol: Optional[float] = None,
) -> None:
    """
    Validate and save state atomically after adding sniper lot(s).
    If journal_path and created_lot are provided, appends LOT_CREATED (with entry_price, token_raw, buy_sol, source) and BUY_CONFIRMED.
    """
    save_state_atomic(state_path, state)
    if journal_path and created_lot:
        payload = {
            "mint": created_lot.mint[:12] if created_lot.mint else None,
            "lot_id": (created_lot.lot_id[:8] if created_lot.lot_id else None),
            "tx_signature": (created_lot.tx_signature[:16] if created_lot.tx_signature else None),
            "entry_price": getattr(created_lot, "entry_price_sol_per_token", None),
            "source": "sniper",
        }
        if token_raw is not None:
            payload["token_raw"] = token_raw
        if buy_sol is not None:
            payload["buy_sol"] = buy_sol
        append_event(journal_path, EVENT_LOT_CREATED, payload)
        if created_lot.tx_signature:
            append_event(
                journal_path,
                BUY_CONFIRMED,
                {
                    "mint": created_lot.mint[:12] if created_lot.mint else None,
                    "lot_id": (created_lot.lot_id[:8] if created_lot.lot_id else None),
                    "tx_signature": created_lot.tx_signature[:16],
                },
            )
