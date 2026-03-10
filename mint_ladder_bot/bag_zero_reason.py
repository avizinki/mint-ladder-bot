from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict


def classify_bag_zero_reason(mint_data: Dict[str, Any], wallet_balance_raw: int) -> str:
    """
    Canonical classifier explaining why a mint with wallet_balance_raw > 0 has trading_bag_raw == 0.

    Priority:
    a) paused_or_quarantine
    b) excluded_from_ladder
    c) unknown_entry_lots
    d) non_tradable_sources_only
    e) other
    """
    try:
        bag = int(mint_data.get("trading_bag_raw") or 0)
    except (ValueError, TypeError):
        bag = 0
    if wallet_balance_raw <= 0 or bag > 0:
        return "other"

    # Paused or quarantine has highest priority.
    failures = mint_data.get("failures") or {}
    paused_until = failures.get("paused_until")
    now = datetime.now(tz=timezone.utc)
    if paused_until:
        try:
            if isinstance(paused_until, str):
                # When coming from state dict, paused_until may already be datetime; dashboard truth
                # normally passes it through unchanged, so we only handle datetime or naive string.
                # For strings we conservatively treat any non-empty value as "paused".
                pass
            return "paused_or_quarantine"
        except Exception:
            return "paused_or_quarantine"
    protection_state = mint_data.get("protection_state")
    quarantine_until = mint_data.get("quarantine_until")
    if protection_state == "quarantine" and quarantine_until:
        return "paused_or_quarantine"

    # Count remaining inventory by source.
    lots = mint_data.get("lots") or []
    tx_derived = 0
    bootstrap_only = 0
    transfer_like = 0
    for lot in lots:
        if not isinstance(lot, dict):
            continue
        status = lot.get("status") or "active"
        if status != "active":
            continue
        try:
            rem = int(lot.get("remaining_amount") or 0)
        except (ValueError, TypeError):
            rem = 0
        src = (lot.get("source") or "").strip()
        if src in ("tx_exact", "tx_parsed"):
            tx_derived += rem
        elif src in ("bootstrap_snapshot", "initial_migration", "snapshot"):
            bootstrap_only += rem
        else:
            transfer_like += rem

    # Entry/policy exclusion: mint marked non-tradable even though tx-derived inventory exists.
    tradable_flag = mint_data.get("tradable")
    if tx_derived > 0 and tradable_flag is False:
        return "excluded_from_ladder"

    # Unknown-entry tx-derived lots: inventory exists but not tradable due to entry constraints.
    if tx_derived > 0:
        return "unknown_entry_lots"

    # Only bootstrap / unknown-source inventory remains.
    if tx_derived == 0 and (bootstrap_only > 0 or transfer_like > 0):
        return "non_tradable_sources_only"

    return "other"

