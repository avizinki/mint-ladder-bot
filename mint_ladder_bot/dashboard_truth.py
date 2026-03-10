"""
Single source of truth for dashboard/status lot-source and pending logic.

Derives normalized token position status, lot-source breakdown, pending counts,
source badges, and alerts from state (+ optional status) so dashboard_server,
validate_dashboard_alignment, and status_snapshot do not duplicate logic.

Blockchain truth → state truth → this layer → normalized status/dashboard → presentation.

---- Sellable contract (no single overloaded "sellable" field) ----

A. lot_remaining_raw
   Sum of remaining_amount across all lots for the token (inventory from ledger).

B. tx_derived_sellable_raw
   Amount remaining from tradable real lots only (tx_exact + tx_parsed).
   Excludes bootstrap_snapshot and transfer_received_unknown unless explicitly intended.

C. runtime_tradable_raw
   Amount the runtime is currently willing to trade (state.trading_bag_raw).
   May reflect trading_bag policy, dust, restrictions, cooldown, etc.

D. dashboard_display_sellable_raw
   Value the operator sees in the main table: runtime_tradable_raw when the runtime
   has set a trading bag; otherwise lot_remaining_raw for inventory visibility.
   Source is sellable_source.

sellable_source: "trading_bag" | "tx_derived" | "lot_sum" | "none"
  - trading_bag: main table shows runtime_tradable_raw (what bot will trade now).
  - tx_derived: display is from tx-derived lots only (rare as main display).
  - lot_sum: display is lot_remaining_raw (no trading_bag set, e.g. bootstrap-only).
  - none: no sellable amount to show.

Bootstrap-only tokens: may have lot_remaining_raw > 0 but runtime_tradable_raw = 0
because the runtime does not allocate trading_bag to bootstrap-only until confirmed.
That is expected; sellable_source explains the difference.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from .bag_zero_reason import classify_bag_zero_reason

logger = logging.getLogger(__name__)

# ---- Sellable source labels (contract) ----
SELLABLE_SOURCE_TRADING_BAG = "trading_bag"
SELLABLE_SOURCE_TX_DERIVED = "tx_derived"
SELLABLE_SOURCE_LOT_SUM = "lot_sum"
SELLABLE_SOURCE_NONE = "none"

# ---- Constants ----

ENTRY_PRICE_MIN = 1e-12
ENTRY_PRICE_MAX = 1e3

POSITION_STATUS_OPEN = "open"
POSITION_STATUS_PARTIAL = "partial"
POSITION_STATUS_CLOSED = "closed"
POSITION_STATUS_BOOTSTRAP_ONLY = "bootstrap-only"
POSITION_STATUS_RECEIVED_ONLY = "received-only"
POSITION_STATUS_BLOCKED = "blocked"
POSITION_STATUS_DUST = "dust"
POSITION_STATUS_UNKNOWN = "unknown"
POSITION_STATUS_ERROR = "error"

PositionStatus = Literal[
    "open", "partial", "closed", "bootstrap-only", "received-only",
    "blocked", "dust", "unknown", "error",
]

LOT_SOURCE_TX_EXACT = "tx_exact"
LOT_SOURCE_TX_PARSED = "tx_parsed"
LOT_SOURCE_BOOTSTRAP_SNAPSHOT = "bootstrap_snapshot"
LOT_SOURCE_TRANSFER_UNKNOWN = "transfer_received_unknown"

SOURCE_CATEGORY_TX_DERIVED = "tx_derived"
SOURCE_CATEGORY_BOOTSTRAP = "bootstrap"
SOURCE_CATEGORY_TRANSFER_UNKNOWN = "transfer_unknown"

ENTRY_CONFIDENCE_DISPLAY = {
    "unknown": "received (price unknown)",
    "pending_price_resolution": "resolving entry price",
    "snapshot": "wallet snapshot",
    "exact": "exact tx price",
    "inferred": "inferred from tx",
}
ENTRY_CONFIDENCE_TOOLTIP = {
    "unknown": "Token balance increased but the originating transaction could not be reconstructed.",
    "pending_price_resolution": "Entry price is being resolved from chain transactions.",
    "snapshot": "Lot created from wallet balance snapshot.",
    "exact": "Entry price taken from the matching on-chain buy transaction.",
    "inferred": "Entry price inferred from transaction data.",
}


# ---- Entry price ----

def entry_price_valid(ep: Any) -> bool:
    """True if entry price is in valid trading range. Use for N/A vs value display."""
    if ep is None:
        return False
    try:
        v = float(ep)
        return v > 0 and ENTRY_PRICE_MIN <= v <= ENTRY_PRICE_MAX
    except (TypeError, ValueError):
        return False


def normalize_entry_price(ep: Any) -> Optional[float]:
    """Return float entry price or None if invalid/zero."""
    if ep is None:
        return None
    try:
        v = float(ep)
        if v == 0 or v != v:
            return None
        return v if entry_price_valid(v) else None
    except (TypeError, ValueError):
        return None


# ---- Pending lot count (single rule for state + API + validation) ----

def is_pending_lot(lot: Dict[str, Any]) -> bool:
    """
    True if lot counts as "pending" for display/alignment.
    Rule: entry_confidence == pending_price_resolution OR (snapshot and source != initial_migration).
    """
    if not isinstance(lot, dict):
        return False
    if lot.get("status") == "duplicate_explained":
        return False
    ec = lot.get("entry_confidence")
    src = lot.get("source") or ""
    if ec == "pending_price_resolution":
        return True
    if ec == "snapshot" and src != "initial_migration":
        return True
    return False


def pending_lots_count_from_state(state: Dict[str, Any]) -> int:
    """Total count of pending lots across all mints. Single source for validation and dashboard."""
    count = 0
    mints = state.get("mints") or {}
    if not isinstance(mints, dict):
        return 0
    for _mint, md in mints.items():
        if not isinstance(md, dict):
            continue
        for lot in md.get("lots") or []:
            if is_pending_lot(lot):
                count += 1
    return count


# ---- Lot source breakdown ----

def lot_source_category(src: str) -> str:
    """Map lot source to category: tx_derived | bootstrap | transfer_unknown."""
    if src in (LOT_SOURCE_TX_EXACT, LOT_SOURCE_TX_PARSED):
        return SOURCE_CATEGORY_TX_DERIVED
    if src in (LOT_SOURCE_BOOTSTRAP_SNAPSHOT, "initial_migration", "snapshot"):
        return SOURCE_CATEGORY_BOOTSTRAP
    return SOURCE_CATEGORY_TRANSFER_UNKNOWN


def source_type_from_lot(lot: Dict[str, Any]) -> str:
    """Normalized source_type for display (tx_swap_exact | tx_parsed | bootstrap_snapshot | transfer_received_unknown)."""
    src = (lot.get("source") or "").strip()
    st = lot.get("source_type")
    if st:
        return st
    if src in (LOT_SOURCE_TX_EXACT, LOT_SOURCE_TX_PARSED):
        return "tx_swap_exact" if src == LOT_SOURCE_TX_EXACT else "tx_parsed"
    if src in (LOT_SOURCE_BOOTSTRAP_SNAPSHOT, "initial_migration", "snapshot"):
        return LOT_SOURCE_BOOTSTRAP_SNAPSHOT
    return LOT_SOURCE_TRANSFER_UNKNOWN


def lot_source_breakdown(lots: List[Dict[str, Any]]) -> Tuple[int, int, int, Dict[str, int]]:
    """
    From list of lot dicts (excluding duplicate_explained), return:
    tx_derived_raw, bootstrap_raw, transfer_unknown_raw,
    and counts_by_source: { tx_exact, tx_parsed, bootstrap_snapshot, transfer_received_unknown }.
    """
    tx_derived_raw = bootstrap_raw = transfer_unknown_raw = 0
    counts: Dict[str, int] = {
        LOT_SOURCE_TX_EXACT: 0,
        LOT_SOURCE_TX_PARSED: 0,
        LOT_SOURCE_BOOTSTRAP_SNAPSHOT: 0,
        LOT_SOURCE_TRANSFER_UNKNOWN: 0,
    }
    for lot in lots or []:
        if not isinstance(lot, dict) or lot.get("status") == "duplicate_explained":
            continue
        try:
            rem = int(lot.get("remaining_amount") or 0)
        except (ValueError, TypeError):
            rem = 0
        src = (lot.get("source") or "").strip()
        if src in (LOT_SOURCE_TX_EXACT, LOT_SOURCE_TX_PARSED):
            tx_derived_raw += rem
            counts[src] = counts.get(src, 0) + 1
        elif src in (LOT_SOURCE_BOOTSTRAP_SNAPSHOT, "initial_migration", "snapshot"):
            bootstrap_raw += rem
            counts[LOT_SOURCE_BOOTSTRAP_SNAPSHOT] = counts.get(LOT_SOURCE_BOOTSTRAP_SNAPSHOT, 0) + 1
        else:
            transfer_unknown_raw += rem
            counts[LOT_SOURCE_TRANSFER_UNKNOWN] = counts.get(LOT_SOURCE_TRANSFER_UNKNOWN, 0) + 1
    return tx_derived_raw, bootstrap_raw, transfer_unknown_raw, counts


def entry_confidence_display(ec: Optional[str]) -> str:
    """Display label for entry_confidence. Never empty; default 'unknown'."""
    if not ec or not ec.strip():
        return "unknown"
    return ENTRY_CONFIDENCE_DISPLAY.get(ec, ec)


def entry_confidence_tooltip(ec: Optional[str]) -> str:
    return ENTRY_CONFIDENCE_TOOLTIP.get(ec or "unknown", "")


# ---- Position status ----

def position_status(
    balance_raw: int,
    sold_raw: int,
    tx_derived_raw: int,
    bootstrap_raw: int,
    transfer_unknown_raw: int,
    cooldown_until: Any = None,
    paused_until: Any = None,
    min_dust_raw: int = 0,
) -> PositionStatus:
    """
    Normalized position status for one token.
    Uses same rule as dashboard: closed / partial / blocked / bootstrap-only / received-only / open / dust / unknown.
    """
    if balance_raw <= 0 and sold_raw > 0:
        return POSITION_STATUS_CLOSED
    if sold_raw > 0 and balance_raw > 0:
        return POSITION_STATUS_PARTIAL
    if paused_until or cooldown_until:
        return POSITION_STATUS_BLOCKED
    if tx_derived_raw == 0 and bootstrap_raw > 0 and transfer_unknown_raw == 0:
        return POSITION_STATUS_BOOTSTRAP_ONLY
    if tx_derived_raw == 0 and bootstrap_raw == 0 and transfer_unknown_raw > 0:
        return POSITION_STATUS_RECEIVED_ONLY
    if balance_raw > 0 and balance_raw <= min_dust_raw:
        return POSITION_STATUS_DUST
    if balance_raw > 0:
        return POSITION_STATUS_OPEN
    return POSITION_STATUS_UNKNOWN


# ---- Per-lot display row (for recent_buys / holdings) ----

def lot_display_row(
    lot: Dict[str, Any],
    mint_addr: str,
    symbol: Optional[str] = None,
    decimals: Optional[int] = None,
) -> Dict[str, Any]:
    """One normalized lot row for dashboard (recent_buys / lots_and_holdings)."""
    if not isinstance(lot, dict):
        return {}
    if lot.get("status") == "duplicate_explained":
        return {}
    ep = lot.get("entry_price_sol_per_token")
    if ep is not None and (ep == 0 or ep == 0.0):
        ep = None
    ec = lot.get("entry_confidence")
    src = lot.get("source") or ""
    if ec == "snapshot" and src != "initial_migration":
        ec = "pending_price_resolution"
    if not ec or not str(ec).strip():
        ec = "unknown"
    valid = entry_price_valid(ep)
    source_type_val = source_type_from_lot(lot)
    source_category = lot_source_category(src)
    return {
        "mint": mint_addr,
        "symbol": symbol,
        "decimals": decimals,
        "lot_id": lot.get("lot_id"),
        "detected_at": lot.get("detected_at"),
        "token_amount": lot.get("token_amount"),
        "remaining_amount": lot.get("remaining_amount"),
        "entry_price_sol_per_token": ep if valid else None,
        "entry_price_valid": valid,
        "entry_price_display": str(ep) if valid else ("N/A (invalid)" if ep is not None else None),
        "source": src,
        "source_type": source_type_val,
        "source_category": source_category,
        "entry_confidence": ec,
        "entry_confidence_display": entry_confidence_display(ec),
        "entry_confidence_tooltip": entry_confidence_tooltip(ec),
        "cost_basis_confidence": lot.get("cost_basis_confidence"),
        "tx_signature": lot.get("tx_signature"),
        "status": lot.get("status"),
        "program_or_venue": lot.get("program_or_venue"),
    }


# ---- Token-level truth (position + breakdown + alerts) ----


def _sold_bot_raw_from_mint_data(mint_data: Dict[str, Any]) -> int:
    """CEO: sold by ladder engine. From sold_bot_raw if set, else sum of executed_steps where key not ext_."""
    if mint_data.get("sold_bot_raw") is not None:
        try:
            return int(mint_data["sold_bot_raw"] or 0)
        except (TypeError, ValueError):
            pass
    steps = mint_data.get("executed_steps") or {}
    total = 0
    for k, s in steps.items():
        if isinstance(k, str) and k.startswith("ext_"):
            continue
        try:
            total += int((s.get("sold_raw") if isinstance(s, dict) else getattr(s, "sold_raw", 0)) or 0)
        except (TypeError, ValueError):
            pass
    return total


def _sold_external_raw_from_mint_data(mint_data: Dict[str, Any]) -> int:
    """CEO: sold by wallet outside bot (EXTERNAL_SELL_INGESTED). From sold_external_raw if set, else sum of ext_ steps."""
    if mint_data.get("sold_external_raw") is not None:
        try:
            return int(mint_data["sold_external_raw"] or 0)
        except (TypeError, ValueError):
            pass
    steps = mint_data.get("executed_steps") or {}
    total = 0
    for k, s in steps.items():
        if not (isinstance(k, str) and k.startswith("ext_")):
            continue
        try:
            total += int((s.get("sold_raw") if isinstance(s, dict) else getattr(s, "sold_raw", 0)) or 0)
        except (TypeError, ValueError):
            pass
    return total


def token_truth(
    mint_addr: str,
    mint_data: Dict[str, Any],
    status_mint: Optional[Dict[str, Any]],
    decimals: int = 6,
    symbol: Optional[str] = None,
    sold_raw_from_steps: int = 0,
) -> Dict[str, Any]:
    """
    Normalized truth for one token: position_status, source breakdown, sellable, entry,
    next_action hint, and alerts (missing_entry, only_bootstrap, zero_sellable, unknown_only, etc.).
    """
    lots = mint_data.get("lots") or []
    tx_derived_raw, bootstrap_raw, transfer_unknown_raw, counts_by_source = lot_source_breakdown(lots)
    balance_raw = 0
    if status_mint and status_mint.get("balance_raw") is not None:
        try:
            balance_raw = int(status_mint["balance_raw"])
        except (ValueError, TypeError):
            pass
    if balance_raw == 0 and mint_data.get("last_known_balance_raw"):
        try:
            balance_raw = int(mint_data["last_known_balance_raw"])
        except (ValueError, TypeError):
            pass
    cooldown = mint_data.get("cooldown_until")
    paused = (mint_data.get("failures") or {}).get("paused_until")
    # Display-layer normalization: expired pauses must not show as active.
    # If paused_until is in the past (or now), treat as not paused.
    if paused:
        try:
            if isinstance(paused, str):
                ts = paused
                if ts.endswith("Z"):
                    ts = ts.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts)
            else:
                dt = paused
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= dt:
                paused = None
        except Exception:
            # On parse failure, fall back to raw value (do not guess).
            pass
    pos_status = position_status(
        balance_raw, sold_raw_from_steps, tx_derived_raw, bootstrap_raw, transfer_unknown_raw,
        cooldown_until=cooldown, paused_until=paused,
    )
    # Entry resolution order: 1) working_entry, 2) mint entry_price, 3) first active lot with valid entry, 4) status bootstrap.
    # When some lots have unknown entry (tx_parsed/tx_exact with null entry), never show market/bootstrap as position entry.
    has_unknown_entry_lots = any(
        isinstance(lot, dict) and lot.get("status") not in ("fully_sold", "duplicate_explained")
        and not entry_price_valid(lot.get("entry_price_sol_per_token"))
        and lot.get("source") in ("tx_exact", "tx_parsed")
        for lot in (lots or [])
    )
    entry_sol = normalize_entry_price(mint_data.get("working_entry_price_sol_per_token"))
    if entry_sol is None:
        entry_sol = normalize_entry_price(mint_data.get("entry_price_sol_per_token"))
    entry_from_lot = False
    # 3) Any active lot with valid entry (never leave ENTRY empty when a valid lot entry exists).
    if entry_sol is None and lots:
        for lot in lots:
            if not isinstance(lot, dict):
                continue
            if lot.get("status") in ("fully_sold", "duplicate_explained"):
                continue
            ep = lot.get("entry_price_sol_per_token")
            if entry_price_valid(ep):
                entry_sol = normalize_entry_price(ep)
                entry_from_lot = True
                break
    # 4) Bootstrap/status entry.
    if entry_sol is None and isinstance(status_mint, dict) and isinstance(status_mint.get("entry"), dict):
        entry_sol = normalize_entry_price(status_mint["entry"].get("entry_price_sol_per_token"))
    # Do not render market/bootstrap as entry when we have lots with unknown entry (invariant: never show market as entry for those).
    if (
        entry_sol is not None
        and has_unknown_entry_lots
        and not entry_from_lot
        and (
            mint_data.get("entry_source") == "market_bootstrap"
            or mint_data.get("bootstrap_from_market")
            or (isinstance(status_mint, dict) and (status_mint.get("entry") or {}).get("entry_source") == "market_bootstrap")
        )
    ):
        entry_sol = None
    if entry_sol is None and lots and balance_raw > 0:
        logger.warning(
            "ENTRY_RESOLUTION_NONE mint=%s balance_raw=%s lots=%s (mint entry missing; no valid lot entry used)",
            mint_addr[:12], balance_raw, len(lots),
        )

    # Sellable contract: separate fields, no overloaded "sellable"
    lot_remaining_raw = tx_derived_raw + bootstrap_raw + transfer_unknown_raw
    tx_derived_sellable_raw = tx_derived_raw  # tradable real lots only
    runtime_tradable_raw = int(mint_data.get("trading_bag_raw") or 0)
    # Manual override (runtime-only fields; optional)
    manual_override_tradable_raw = 0
    try:
        manual_override_tradable_raw = int(mint_data.get("manual_override_tradable_raw") or 0)
    except (TypeError, ValueError):
        manual_override_tradable_raw = 0

    if runtime_tradable_raw > 0:
        dashboard_display_sellable_raw = runtime_tradable_raw
        sellable_source = SELLABLE_SOURCE_TRADING_BAG
    elif tx_derived_sellable_raw > 0:
        dashboard_display_sellable_raw = tx_derived_sellable_raw
        sellable_source = SELLABLE_SOURCE_TX_DERIVED
    elif lot_remaining_raw > 0:
        dashboard_display_sellable_raw = lot_remaining_raw
        sellable_source = SELLABLE_SOURCE_LOT_SUM
    else:
        dashboard_display_sellable_raw = 0
        sellable_source = SELLABLE_SOURCE_NONE

    market = (status_mint or {}).get("market") or {}
    dexscreener = market.get("dexscreener") if isinstance(market, dict) else {}
    liquidity_usd = None
    if isinstance(dexscreener, dict) and dexscreener.get("liquidity_usd") is not None:
        try:
            liquidity_usd = float(dexscreener["liquidity_usd"])
        except (TypeError, ValueError):
            pass
    current_price_sol = None
    if isinstance(dexscreener, dict) and dexscreener.get("price_native") is not None:
        try:
            current_price_sol = float(dexscreener["price_native"])
        except (TypeError, ValueError):
            pass

    alerts: List[str] = []
    if balance_raw > 0 and entry_sol is None and (tx_derived_raw > 0 or bootstrap_raw > 0 or transfer_unknown_raw > 0):
        alerts.append("missing_entry")
    if balance_raw > 0 and tx_derived_raw == 0 and bootstrap_raw > 0 and transfer_unknown_raw == 0:
        alerts.append("only_bootstrap_lots")
    if balance_raw > 0 and runtime_tradable_raw == 0:
        alerts.append("zero_runtime_tradable")
    if balance_raw > 0 and tx_derived_sellable_raw == 0 and lot_remaining_raw > 0:
        alerts.append("no_tx_derived_sellable")
    if lot_remaining_raw > 0 and dashboard_display_sellable_raw != lot_remaining_raw:
        alerts.append("display_sellable_differs_from_lot_sum")
    if tx_derived_raw > 0 and entry_sol is None:
        alerts.append("tx_derived_missing_entry")
    if balance_raw > 0 and current_price_sol is None:
        alerts.append("no_market_data")
    if liquidity_usd is not None and liquidity_usd < 10_000:
        alerts.append("low_liquidity")
    if balance_raw > 0 and tx_derived_raw == 0 and bootstrap_raw == 0 and transfer_unknown_raw > 0:
        alerts.append("unknown_source_only")

    # External inventory label: mint has balance but no tx-derived lots.
    has_tx_lot = any(
        isinstance(lot, dict)
        and lot.get("status") not in ("fully_sold", "duplicate_explained")
        and lot.get("source") in ("tx_exact", "tx_parsed")
        for lot in (lots or [])
    )
    external_inventory_only = bool(
        balance_raw > 0 and tx_derived_raw == 0 and not has_tx_lot
    )
    # Same-mint external excess: wallet_balance_raw >= tx_proven_raw > 0.
    tx_proven_raw = lot_remaining_raw
    external_excess_raw = max(0, balance_raw - tx_proven_raw) if tx_proven_raw > 0 else 0
    has_external_excess = external_excess_raw > 0
    if external_inventory_only:
        inventory_source_label = "external_transfer"
    elif has_external_excess:
        inventory_source_label = "bot_acquired+external_excess"
    else:
        inventory_source_label = "bot_acquired"

    next_action = "next_sell_target"
    if pos_status == POSITION_STATUS_CLOSED:
        next_action = "fully_sold"
    elif paused:
        next_action = "paused_until"
    elif cooldown:
        next_action = "cooldown_until"
    elif liquidity_usd is not None and liquidity_usd < 10_000:
        next_action = "liquidity_blocked"
    elif current_price_sol is None:
        next_action = "no_market_data"

    # Bag-zero reason: canonical helper only when balance > 0 and runtime bag is zero.
    # Do not invent; runner uses same classify_bag_zero_reason for health sell_readiness.
    bag_zero_reason: Optional[str] = None
    if balance_raw > 0 and runtime_tradable_raw == 0:
        bag_zero_reason = classify_bag_zero_reason(mint_data, balance_raw)

    # Manual override sold (runtime-only; optional)
    manual_override_sold_raw = 0
    try:
        manual_override_sold_raw = int(mint_data.get("manual_override_sold_raw") or 0)
    except (TypeError, ValueError):
        manual_override_sold_raw = 0

    # Paused: from state failures.paused_until only; single source so dashboard never duplicates.
    return {
        "mint": mint_addr,
        "symbol": symbol or mint_addr[:8],
        "decimals": decimals,
        "balance_raw": balance_raw,
        "lot_remaining_raw": lot_remaining_raw,
        "entry_unknown": entry_sol is None,
        "tx_derived_raw": tx_derived_raw,
        "bootstrap_raw": bootstrap_raw,
        "transfer_unknown_raw": transfer_unknown_raw,
        "sold_raw": sold_raw_from_steps,
        "sold_bot_raw": _sold_bot_raw_from_mint_data(mint_data),
        "sold_external_raw": _sold_external_raw_from_mint_data(mint_data),
        "tx_derived_sellable_raw": tx_derived_sellable_raw,
        "runtime_tradable_raw": runtime_tradable_raw,
        "manual_override_tradable_raw": manual_override_tradable_raw,
        "manual_override_sold_raw": manual_override_sold_raw,
        "dashboard_display_sellable_raw": dashboard_display_sellable_raw,
        "sellable_source": sellable_source,
        "sellable_raw": dashboard_display_sellable_raw,  # backward compat; prefer explicit fields
        "counts_by_source": counts_by_source,
        "position_status": pos_status,
        "entry_sol_per_token": entry_sol,
        "has_entry": entry_sol is not None,
        "has_market_data": current_price_sol is not None,
        "liquidity_usd": liquidity_usd,
        "alerts": alerts,
        "external_inventory_only": external_inventory_only,
        "inventory_source_label": inventory_source_label,
        "tx_proven_raw": tx_proven_raw,
        "external_excess_raw": external_excess_raw,
        "has_external_excess": has_external_excess,
        "external_excess_mode": (
            "external_excess"
            if has_external_excess and balance_raw >= tx_proven_raw > 0
            else "underwater"
            if tx_proven_raw > balance_raw
            else "none"
        ),
        "next_action": next_action,
        "bag_zero_reason": bag_zero_reason,
        "paused_until": paused,
        "paused": bool(paused),
    }


# ---- Global summary (for logging / KPIs) ----

def global_lot_source_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    From state dict, return global counts: total_lots, total_tokens, by source (tx_exact, tx_parsed,
    bootstrap_snapshot, transfer_received_unknown), pending_lots_count.
    """
    mints = state.get("mints") or {}
    if not isinstance(mints, dict):
        return {
            "total_tokens": 0,
            "total_lots": 0,
            "tx_exact": 0,
            "tx_parsed": 0,
            "bootstrap_snapshot": 0,
            "transfer_received_unknown": 0,
            "pending_lots_count": 0,
        }
    total_lots = 0
    pending = 0
    by_source: Dict[str, int] = {
        LOT_SOURCE_TX_EXACT: 0,
        LOT_SOURCE_TX_PARSED: 0,
        LOT_SOURCE_BOOTSTRAP_SNAPSHOT: 0,
        LOT_SOURCE_TRANSFER_UNKNOWN: 0,
    }
    for _mint, md in mints.items():
        if not isinstance(md, dict):
            continue
        lots = md.get("lots") or []
        for lot in lots:
            if not isinstance(lot, dict) or lot.get("status") == "duplicate_explained":
                continue
            total_lots += 1
            if is_pending_lot(lot):
                pending += 1
            src = (lot.get("source") or "").strip()
            if src == LOT_SOURCE_TX_EXACT:
                by_source[LOT_SOURCE_TX_EXACT] += 1
            elif src == LOT_SOURCE_TX_PARSED:
                by_source[LOT_SOURCE_TX_PARSED] += 1
            elif src in (LOT_SOURCE_BOOTSTRAP_SNAPSHOT, "initial_migration", "snapshot"):
                by_source[LOT_SOURCE_BOOTSTRAP_SNAPSHOT] += 1
            else:
                by_source[LOT_SOURCE_TRANSFER_UNKNOWN] += 1
    return {
        "total_tokens": len(mints),
        "total_lots": total_lots,
        "tx_exact": by_source[LOT_SOURCE_TX_EXACT],
        "tx_parsed": by_source[LOT_SOURCE_TX_PARSED],
        "bootstrap_snapshot": by_source[LOT_SOURCE_BOOTSTRAP_SNAPSHOT],
        "transfer_received_unknown": by_source[LOT_SOURCE_TRANSFER_UNKNOWN],
        "pending_lots_count": pending,
    }
