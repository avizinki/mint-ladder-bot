"""
Structured event journal and safety state for live validation hardening.
- Duplicate detection: persist processed tx signatures and mint-change fingerprints.
- Event journal: append-only JSONL for critical actions.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_PROCESSED_DEFAULT = 5000


def _load_safety_state(path: Path, max_sigs: int, max_fingerprints: int) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {"processed_signatures": [], "processed_fingerprints": []}
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text())
        out["processed_signatures"] = list(data.get("processed_signatures") or [])[-max_sigs:]
        out["processed_fingerprints"] = list(data.get("processed_fingerprints") or [])[-max_fingerprints:]
    except Exception as exc:
        logger.warning("Failed to load safety_state %s: %s", path, exc)
    return out


def _save_safety_state(path: Path, data: Dict[str, List[str]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        logger.warning("Failed to save safety_state %s: %s", path, exc)


def is_duplicate_fingerprint(path: Path, fingerprint: str, max_fingerprints: int = MAX_PROCESSED_DEFAULT) -> bool:
    state = _load_safety_state(path, 0, max_fingerprints)
    return fingerprint in (state.get("processed_fingerprints") or [])


def is_duplicate_signature(path: Path, signature: str, max_sigs: int = MAX_PROCESSED_DEFAULT) -> bool:
    state = _load_safety_state(path, max_sigs, 0)
    return signature in (state.get("processed_signatures") or [])


def add_processed_fingerprint(path: Path, fingerprint: str, max_fingerprints: int = MAX_PROCESSED_DEFAULT) -> None:
    state = _load_safety_state(path, 0, max_fingerprints)
    lst = state.get("processed_fingerprints") or []
    if fingerprint not in lst:
        lst = (lst + [fingerprint])[-max_fingerprints:]
    state["processed_fingerprints"] = lst
    _save_safety_state(path, state)


def add_processed_signature(path: Path, signature: str, max_sigs: int = MAX_PROCESSED_DEFAULT) -> None:
    state = _load_safety_state(path, max_sigs, 0)
    lst = state.get("processed_signatures") or []
    if signature not in lst:
        lst = (lst + [signature])[-max_sigs:]
    state["processed_signatures"] = lst
    _save_safety_state(path, state)


def make_fingerprint(mint: str, delta_raw: int, slot: int) -> str:
    return f"{mint}:{delta_raw}:{slot}"


def make_unresolved_delta_fingerprint(mint: str, delta_raw: int) -> str:
    """Stable dedup key for 'unmatched balance delta' so we only emit and retry once per (mint, delta)."""
    return f"unresolved:{mint}:{delta_raw}"


# Structured log events (CEO directive §7)
BOT_START = "BOT_START"
CYCLE_START = "CYCLE_START"
CYCLE_SUMMARY = "CYCLE_SUMMARY"
AUDIT_SELL = "AUDIT_SELL"
AUDIT_BUYBACK = "AUDIT_BUYBACK"
ENTRY_RESOLUTION = "ENTRY_RESOLUTION"
EXECUTION_FAILED = "EXECUTION_FAILED"
RPC_FAILOVER = "RPC_FAILOVER"
MINT_PAUSED = "MINT_PAUSED"
ENTRY_PRICE_INVALID = "ENTRY_PRICE_INVALID"
RISK_BLOCK = "RISK_BLOCK"

# Event types for journal
EVENT_MINT_DETECTED = "MINT_DETECTED"
EVENT_LOT_CREATED = "LOT_CREATED"
EVENT_LOT_CREATED_TX_EXACT = "LOT_CREATED_TX_EXACT"
EVENT_LOT_CREATED_TX_PARSED = "LOT_CREATED_TX_PARSED"
EVENT_LOT_CREATED_SNAPSHOT = "LOT_CREATED_SNAPSHOT"
EVENT_BUY_PRICE_UNKNOWN = "BUY_PRICE_UNKNOWN"
EVENT_BUY_BACKFILL_CREATED = "BUY_BACKFILL_CREATED"
EVENT_BUY_BACKFILL_SKIPPED = "BUY_BACKFILL_SKIPPED"
EVENT_PROTECTION_ARMED = "PROTECTION_ARMED"
EVENT_TP_HIT = "TP_HIT"
EVENT_STOP_HIT = "STOP_HIT"
EVENT_SELL_SENT = "SELL_SENT"
EVENT_SELL_CONFIRMED = "SELL_CONFIRMED"
EVENT_SELL_FAILED = "SELL_FAILED"
EVENT_RECONCILED = "RECONCILED"
EVENT_CIRCUIT_BREAKER = "CIRCUIT_BREAKER"

# Tx-first lot engine
EVENT_BUY_TX_INGESTED = "BUY_TX_INGESTED"
EVENT_LOT_CREATED_FROM_TX = "LOT_CREATED_FROM_TX"
EVENT_TX_ALREADY_PROCESSED = "TX_ALREADY_PROCESSED"
EVENT_TX_PARSE_FAILED = "TX_PARSE_FAILED"
EVENT_BALANCE_INCREASE_UNMATCHED = "BALANCE_INCREASE_UNMATCHED"
EVENT_SNAPSHOT_FALLBACK_USED = "SNAPSHOT_FALLBACK_USED"
EVENT_BUY_DETECTED_NO_TX = "BUY_DETECTED_NO_TX"
# Unresolved balance delta: one event per (mint, delta); no lot created; informational only
EVENT_UNRESOLVED_BALANCE_DELTA = "UNRESOLVED_BALANCE_DELTA"
# Duplicate prevention (ledger integrity)
EVENT_DUPLICATE_TX_LOT_SKIPPED = "DUPLICATE_TX_LOT_SKIPPED"
EVENT_DUPLICATE_FALLBACK_LOT_SKIPPED = "DUPLICATE_FALLBACK_LOT_SKIPPED"
EVENT_DUPLICATE_LOT_CLEANED = "DUPLICATE_LOT_CLEANED"
# Token→token source disposal (audit only)
EVENT_TOKEN_TO_TOKEN_SOURCE_DISPOSED = "TOKEN_TO_TOKEN_SOURCE_DISPOSED"

# Sell accounting: bot vs external (CEO: invariant sold_raw == sold_bot_raw + sold_external_raw)
EVENT_BOT_SELL_ACCOUNTED = "BOT_SELL_ACCOUNTED"
EVENT_EXTERNAL_SELL_ACCOUNTED = "EXTERNAL_SELL_ACCOUNTED"
EVENT_SELL_ACCOUNTING_INVARIANT_BROKEN = "SELL_ACCOUNTING_INVARIANT_BROKEN"

# Reconciliation-based per-mint pause / recovery (wallet vs lots mismatch).
EVENT_MINT_RECONCILIATION_PAUSED = "MINT_RECONCILIATION_PAUSED"
EVENT_MINT_RECONCILIATION_RECOVERED = "MINT_RECONCILIATION_RECOVERED"

# Lot entry reconstruction (invariant: only tx-derived quote value; never market/bootstrap)
LOT_ENTRY_SET_FROM_SOL_DELTA = "LOT_ENTRY_SET_FROM_SOL_DELTA"

# Canonical event names (Avizinki Master Execution Directive — observability)
LAUNCH_DETECTED = "LAUNCH_DETECTED"
OPPORTUNITY_NORMALIZED = "OPPORTUNITY_NORMALIZED"
TOKEN_FILTER_REJECTED = "TOKEN_FILTER_REJECTED"
TOKEN_FILTER_APPROVED = "TOKEN_FILTER_APPROVED"
BUY_SENT = "BUY_SENT"
BUY_CONFIRMED = "BUY_CONFIRMED"
BUY_FAILED = "BUY_FAILED"
# LOT_CREATED already exists as EVENT_LOT_CREATED; use both for compatibility
LOT_REBUILT = "LOT_REBUILT"
LADDER_ARMED = "LADDER_ARMED"
LADDER_LEVEL_TRIGGERED = "LADDER_LEVEL_TRIGGERED"
# SELL_* already exist as EVENT_SELL_*
EXTERNAL_SELL_INGESTED = "EXTERNAL_SELL_INGESTED"
REBUILD_STARTED = "REBUILD_STARTED"
REBUILD_COMPLETED = "REBUILD_COMPLETED"
INVARIANT_WARNING = "INVARIANT_WARNING"
PROVIDER_WARNING = "PROVIDER_WARNING"
DASHBOARD_UPDATED = "DASHBOARD_UPDATED"
LOT_ENTRY_SET_FROM_WSOL_EQUIV = "LOT_ENTRY_SET_FROM_WSOL_EQUIV"
LOT_ENTRY_SET_FROM_SOURCE_FIFO_COST = "LOT_ENTRY_SET_FROM_SOURCE_FIFO_COST"
LOT_ENTRY_LEFT_UNKNOWN = "LOT_ENTRY_LEFT_UNKNOWN"
LOT_ENTRY_REPAIR_CLEARED_MARKET_FALLBACK = "LOT_ENTRY_REPAIR_CLEARED_MARKET_FALLBACK"

# External wallet activity observability / invariants
EXTERNAL_TX_OBSERVED = "EXTERNAL_TX_OBSERVED"
UNCLASSIFIED_EXTERNAL_TX = "UNCLASSIFIED_EXTERNAL_TX"
UNEXPLAINED_WALLET_CHANGE = "UNEXPLAINED_WALLET_CHANGE"

# Sniper Phase 1 (manual-seed sniper) event types
SNIPER_CANDIDATE_DISCOVERED = "SNIPER_CANDIDATE_DISCOVERED"
SNIPER_CANDIDATE_NORMALIZED = "SNIPER_CANDIDATE_NORMALIZED"
SNIPER_CANDIDATE_REJECTED = "SNIPER_CANDIDATE_REJECTED"
SNIPER_CANDIDATE_SCORED = "SNIPER_CANDIDATE_SCORED"
SNIPER_BUY_REQUESTED = "SNIPER_BUY_REQUESTED"
SNIPER_BUY_QUOTE_ACCEPTED = "SNIPER_BUY_QUOTE_ACCEPTED"
SNIPER_BUY_QUOTE_REJECTED = "SNIPER_BUY_QUOTE_REJECTED"
SNIPER_BUY_SUBMITTED = "SNIPER_BUY_SUBMITTED"
SNIPER_BUY_OBSERVED = "SNIPER_BUY_OBSERVED"
SNIPER_BUY_CONFIRMED = "SNIPER_BUY_CONFIRMED"
SNIPER_BUY_FAILED = "SNIPER_BUY_FAILED"
SNIPER_BUY_UNCERTAIN = "SNIPER_BUY_UNCERTAIN"
SNIPER_PENDING_RESOLVED = "SNIPER_PENDING_RESOLVED"
SNIPER_LOT_ARMED = "SNIPER_LOT_ARMED"
SNIPER_DUPLICATE_BLOCKED = "SNIPER_DUPLICATE_BLOCKED"
SNIPER_COOLDOWN_BLOCKED = "SNIPER_COOLDOWN_BLOCKED"
SNIPER_RISK_BLOCKED = "SNIPER_RISK_BLOCKED"

# Manual override inventory lifecycle (explicit operator-approved non-tx-proven inventory)
MANUAL_OVERRIDE_CREATED = "MANUAL_OVERRIDE_CREATED"
MANUAL_OVERRIDE_UPDATED = "MANUAL_OVERRIDE_UPDATED"
MANUAL_OVERRIDE_DELETED = "MANUAL_OVERRIDE_DELETED"
# Consumption of manual override inventory during trading.
MANUAL_OVERRIDE_CONSUMED = "MANUAL_OVERRIDE_CONSUMED"
# Manual override reconciliation bypass (per-mint, operator-approved)
MANUAL_OVERRIDE_BYPASS_ENABLED = "MANUAL_OVERRIDE_BYPASS_ENABLED"
MANUAL_OVERRIDE_BYPASS_DISABLED = "MANUAL_OVERRIDE_BYPASS_DISABLED"


def append_event(
    journal_path: Path,
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one JSON line to the event journal."""
    if not journal_path:
        return
    try:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "event": event_type,
            **(payload or {}),
        }
        with open(journal_path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        logger.warning("Failed to append event %s to %s: %s", event_type, journal_path, exc)


def read_events(journal_path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Read event journal lines (newest last). If limit, return last N lines."""
    if not journal_path.exists():
        return []
    lines = journal_path.read_text().strip().splitlines()
    if not lines:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    if limit is not None and len(out) > limit:
        out = out[-limit:]
    return out
