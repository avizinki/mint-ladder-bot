"""
Runtime monitoring for mint-ladder-bot: read-only parsing of state.json and run.log.

Produces wallet exposure summary, failure/abnormal detection, and cooldown triggers.
No PnL. No writes to state or log.

Spec: docs/trading/mint-ladder-bot-monitoring-runtime.md
Events/conditions: docs/trading/mint-ladder-bot-monitoring-spec.md
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _parse_iso_datetime(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---- State parsing ----


def load_state(state_path: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Load state from state_path. Returns (state_dict, error_message).
    state_dict is minimal (wallet, mints) on success or empty on missing/malformed;
    error_message is set when file is missing or invalid.
    """
    if not state_path.exists():
        return ({"wallet": None, "mints": {}}, f"Missing file: {state_path}")

    try:
        data = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return ({"wallet": None, "mints": {}}, f"State load failed: {e}")

    if not isinstance(data, dict):
        return ({"wallet": None, "mints": {}}, "State root is not a dict")

    wallet = data.get("wallet") if isinstance(data.get("wallet"), str) else None
    mints = data.get("mints")
    if not isinstance(mints, dict):
        mints = {}

    return ({"wallet": wallet, "mints": mints, "version": data.get("version"), "started_at": data.get("started_at")}, None)


def _mint_failures(mint_data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract failures dict from a mint entry; tolerate missing/nested structure."""
    f = mint_data.get("failures")
    if not isinstance(f, dict):
        return {"count": 0, "last_error": None, "paused_until": None}
    count = f.get("count")
    if not isinstance(count, (int, float)):
        count = 0
    return {
        "count": int(count),
        "last_error": f.get("last_error") if isinstance(f.get("last_error"), str) else None,
        "paused_until": _parse_iso_datetime(f.get("paused_until")),
    }


def _mint_buybacks(mint_data: Dict[str, Any]) -> Dict[str, Any]:
    b = mint_data.get("buybacks")
    if not isinstance(b, dict):
        return {"total_sol_spent": 0.0}
    total = b.get("total_sol_spent")
    if total is None:
        return {"total_sol_spent": 0.0}
    try:
        return {"total_sol_spent": float(total)}
    except (TypeError, ValueError):
        return {"total_sol_spent": 0.0}


# ---- Log parsing (line-by-line / regex) ----


@dataclass
class LogAggregates:
    """Parsed aggregates from run.log."""

    last_run_totals: Optional[Dict[str, int]] = None  # sells_ok, sells_fail, buybacks_ok, buybacks_fail
    last_cycle_summary: Optional[Dict[str, Any]] = None
    stop_file_seen: bool = False
    global_rpc_pause_until: Optional[datetime] = None
    rpc_failures_count: Optional[int] = None
    liquidity_collapse: List[Dict[str, Any]] = field(default_factory=list)  # mint, paused_until, etc.
    mint_paused_lines: List[Dict[str, Any]] = field(default_factory=list)  # mint, paused_until
    confirm_uncertain: List[Dict[str, Any]] = field(default_factory=list)  # mint, step_id, pause_min
    startup_validation_failed: List[Dict[str, Any]] = field(default_factory=list)  # mint, pause_min


# Patterns (from spec and runner).
_RE_RUN_TOTALS = re.compile(
    r"Run totals:\s*sells_ok=(\d+)\s+sells_fail=(\d+)\s+buybacks_ok=(\d+)\s+buybacks_fail=(\d+)"
)
_RE_CYCLE = re.compile(
    r"Cycle \d+ summary:\s*cycle_duration_ms=[\d.]+\s+rpc_latency_ms=[\d.]+\s+"
    r"sells_ok=(\d+)\s+sells_fail=(\d+)\s+buybacks_ok=(\d+)\s+buybacks_fail=(\d+)"
)
_RE_LIQUIDITY_COLLAPSE = re.compile(r"LIQUIDITY_COLLAPSE mint=(\S+)")
_RE_GLOBAL_RPC = re.compile(r"Global trading paused due to RPC instability until (.+?)(?:\.|\s|$)")
_RE_RPC_THRESHOLD = re.compile(
    r"RPC failures \((\d+)\) >= threshold; global trading paused until (.+?)(?:\.|\s|$)"
)
_RE_MINT_PAUSED = re.compile(r"Mint (\S+) is paused until (.+?)(?:\s|$)")
_RE_CONFIRM_UNCERTAIN = re.compile(
    r"Mint (\S+) step_id=(\S+): confirm uncertain; pausing mint for (\d+) min"
)
_RE_STARTUP_VALIDATION = re.compile(
    r"Startup validation: mint (\S+) Jupiter quote failed; pausing mint for (\d+) minutes"
)


def _parse_datetime_from_log(s: str) -> Optional[datetime]:
    """Parse timestamp from log message (runner uses %s for datetime)."""
    s = s.strip().rstrip(".")
    return _parse_iso_datetime(s)


def parse_log(log_path: Path) -> Tuple[LogAggregates, Optional[str]]:
    """
    Parse run.log for cycle summary, run totals, and abnormal-condition lines.
    Returns (LogAggregates, error_message). error_message set only if file missing/unreadable.
    """
    agg = LogAggregates()

    if not log_path.exists():
        return (agg, f"Missing file: {log_path}")

    try:
        text = log_path.read_text()
    except OSError as e:
        return (agg, f"Log read failed: {e}")

    for line in text.splitlines():
        # Run totals (last one wins)
        m = _RE_RUN_TOTALS.search(line)
        if m:
            agg.last_run_totals = {
                "sells_ok": int(m.group(1)),
                "sells_fail": int(m.group(2)),
                "buybacks_ok": int(m.group(3)),
                "buybacks_fail": int(m.group(4)),
            }
            continue

        # Cycle summary (last one wins)
        m = _RE_CYCLE.search(line)
        if m:
            agg.last_cycle_summary = {
                "sells_ok": int(m.group(1)),
                "sells_fail": int(m.group(2)),
                "buybacks_ok": int(m.group(3)),
                "buybacks_fail": int(m.group(4)),
            }
            continue

        if "STOP file present; trading disabled" in line:
            agg.stop_file_seen = True
            continue

        if "LIQUIDITY_COLLAPSE" in line:
            m = _RE_LIQUIDITY_COLLAPSE.search(line)
            mint = m.group(1) if m else None
            # Try to get "pausing until <ts>" from line
            until = None
            if "pausing until" in line:
                parts = line.split("pausing until", 1)
                if len(parts) == 2:
                    until = _parse_datetime_from_log(parts[1].strip())
            agg.liquidity_collapse.append({"mint": mint, "paused_until": until, "line": line.strip()})
            continue

        if "Global trading paused due to RPC instability" in line:
            m = _RE_GLOBAL_RPC.search(line)
            if m:
                agg.global_rpc_pause_until = _parse_datetime_from_log(m.group(1))
            continue

        if "RPC failures" in line and "global trading paused" in line:
            m = _RE_RPC_THRESHOLD.search(line)
            if m:
                agg.rpc_failures_count = int(m.group(1))
                agg.global_rpc_pause_until = _parse_datetime_from_log(m.group(2))
            continue

        if " is paused until " in line and "Mint " in line:
            m = _RE_MINT_PAUSED.search(line)
            if m:
                agg.mint_paused_lines.append({
                    "mint": m.group(1),
                    "paused_until": _parse_datetime_from_log(m.group(2)),
                })
            continue

        if "confirm uncertain; pausing mint" in line:
            m = _RE_CONFIRM_UNCERTAIN.search(line)
            if m:
                agg.confirm_uncertain.append({
                    "mint": m.group(1),
                    "step_id": m.group(2),
                    "pause_min": int(m.group(3)),
                })
            continue

        if "Startup validation:" in line and "Jupiter quote failed" in line:
            m = _RE_STARTUP_VALIDATION.search(line)
            if m:
                agg.startup_validation_failed.append({
                    "mint": m.group(1),
                    "pause_min": int(m.group(2)),
                })
            continue

    return (agg, None)


# ---- Wallet exposure (from state + log) ----


def build_wallet_exposure(
    state: Dict[str, Any],
    log_agg: LogAggregates,
    wallet_id_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Per-wallet aggregates: mint_count, executed_steps_total, failure counts,
    mints_paused, cooldown_active_count, buybacks; session sells_ok/fail from log.
    No PnL.
    """
    wallet_id = wallet_id_override or state.get("wallet") or "unknown"
    mints = state.get("mints") or {}
    now = _now_utc()

    mint_count = len(mints)
    executed_steps_total = 0
    failure_count_total = 0
    mints_paused = 0
    cooldown_active_count = 0
    buybacks_total_sol = 0.0
    buybacks_count = 0  # we don't have per-event count in state; use 0 or derive from log if needed
    executed_steps_per_mint: Dict[str, int] = {}
    failure_count_per_mint: Dict[str, int] = {}

    for mint_id, mint_data in mints.items():
        if not isinstance(mint_data, dict):
            continue

        steps = mint_data.get("executed_steps")
        if isinstance(steps, dict):
            n = len(steps)
            executed_steps_total += n
            executed_steps_per_mint[mint_id] = n
        else:
            executed_steps_per_mint[mint_id] = 0

        failures = _mint_failures(mint_data)
        failure_count_total += failures["count"]
        failure_count_per_mint[mint_id] = failures["count"]

        if failures["paused_until"] and failures["paused_until"] > now:
            mints_paused += 1

        cooldown_until = _parse_iso_datetime(mint_data.get("cooldown_until"))
        if cooldown_until and cooldown_until > now:
            cooldown_active_count += 1

        buybacks = _mint_buybacks(mint_data)
        buybacks_total_sol += buybacks["total_sol_spent"]

    out = {
        "wallet_id": wallet_id,
        "mint_count": mint_count,
        "executed_steps_total": executed_steps_total,
        "executed_steps_per_mint": executed_steps_per_mint,
        "failure_count_total": failure_count_total,
        "failure_count_per_mint": failure_count_per_mint,
        "mints_paused": mints_paused,
        "cooldown_active_count": cooldown_active_count,
        "buybacks_total_sol": round(buybacks_total_sol, 6),
        "buybacks_count": buybacks_count,
    }

    if log_agg.last_run_totals:
        out["sells_ok"] = log_agg.last_run_totals.get("sells_ok", 0)
        out["sells_fail"] = log_agg.last_run_totals.get("sells_fail", 0)
        out["buybacks_ok"] = log_agg.last_run_totals.get("buybacks_ok", 0)
        out["buybacks_fail"] = log_agg.last_run_totals.get("buybacks_fail", 0)
    elif log_agg.last_cycle_summary:
        out["sells_ok"] = log_agg.last_cycle_summary.get("sells_ok", 0)
        out["sells_fail"] = log_agg.last_cycle_summary.get("sells_fail", 0)
        out["buybacks_ok"] = log_agg.last_cycle_summary.get("buybacks_ok", 0)
        out["buybacks_fail"] = log_agg.last_cycle_summary.get("buybacks_fail", 0)
    else:
        out["sells_ok"] = out["sells_fail"] = out["buybacks_ok"] = out["buybacks_fail"] = 0

    return out


# ---- Failure detection (spec §5) ----


def build_failures_and_abnormal(
    state: Dict[str, Any],
    log_agg: LogAggregates,
    failure_count_threshold: int = 3,
) -> List[Dict[str, Any]]:
    """
    List of detected abnormal conditions from spec §5: repeated failures, liquidity collapse,
    RPC global pause, STOP file, confirm uncertain, startup validation failure.
    """
    events: List[Dict[str, Any]] = []
    now = _now_utc()
    mints = state.get("mints") or {}

    # Per-mint repeated failures / paused
    for mint_id, mint_data in mints.items():
        if not isinstance(mint_data, dict):
            continue
        failures = _mint_failures(mint_data)
        if failures["count"] >= failure_count_threshold or failures["paused_until"]:
            events.append({
                "condition": "per_mint_repeated_failures",
                "mint": mint_id,
                "failure_count": failures["count"],
                "paused_until": failures["paused_until"].isoformat() if failures["paused_until"] else None,
                "last_error": failures["last_error"],
            })

    # Liquidity collapse (from state + log)
    for mint_id, mint_data in mints.items():
        if not isinstance(mint_data, dict):
            continue
        failures = _mint_failures(mint_data)
        if failures["last_error"] == "liquidity_collapse" and failures["paused_until"] and failures["paused_until"] > now:
            events.append({
                "condition": "liquidity_collapse",
                "mint": mint_id,
                "paused_until": failures["paused_until"].isoformat(),
            })
    for ev in log_agg.liquidity_collapse:
        events.append({
            "condition": "liquidity_collapse",
            "mint": ev.get("mint"),
            "paused_until": ev["paused_until"].isoformat() if ev.get("paused_until") else None,
            "source": "log",
        })

    # RPC global pause (log only)
    if log_agg.global_rpc_pause_until and log_agg.global_rpc_pause_until > now:
        events.append({
            "condition": "global_rpc_pause",
            "paused_until": log_agg.global_rpc_pause_until.isoformat(),
            "rpc_failures_count": log_agg.rpc_failures_count,
        })

    # STOP file
    if log_agg.stop_file_seen:
        events.append({"condition": "stop_file_active", "reason": "stop_file_active"})

    # Confirm uncertain (log; state already has paused_until in per_mint_repeated_failures)
    for ev in log_agg.confirm_uncertain:
        events.append({
            "condition": "confirm_uncertain",
            "mint": ev.get("mint"),
            "step_id": ev.get("step_id"),
            "pause_min": ev.get("pause_min"),
        })

    # Startup validation failure
    for ev in log_agg.startup_validation_failed:
        events.append({
            "condition": "startup_validation_failed",
            "mint": ev.get("mint"),
            "pause_min": ev.get("pause_min"),
        })

    return events


# ---- Cooldown triggers ----


def build_cooldowns(state: Dict[str, Any], log_agg: LogAggregates) -> Dict[str, Any]:
    """
    per_mint_pause (mint, paused_until, last_error), per_mint_cooldown (mint, cooldown_until),
    global_trading_paused_until from log. Lane cooldowns reserved.
    """
    now = _now_utc()
    mints = state.get("mints") or {}

    per_mint_pause: List[Dict[str, Any]] = []
    for mint_id, mint_data in mints.items():
        if not isinstance(mint_data, dict):
            continue
        failures = _mint_failures(mint_data)
        if failures["paused_until"] and failures["paused_until"] > now:
            per_mint_pause.append({
                "mint": mint_id,
                "paused_until": failures["paused_until"].isoformat(),
                "last_error": failures["last_error"],
            })

    per_mint_cooldown: List[Dict[str, Any]] = []
    for mint_id, mint_data in mints.items():
        if not isinstance(mint_data, dict):
            continue
        cooldown_until = _parse_iso_datetime(mint_data.get("cooldown_until"))
        if cooldown_until and cooldown_until > now:
            per_mint_cooldown.append({
                "mint": mint_id,
                "cooldown_until": cooldown_until.isoformat(),
            })

    global_until = None
    if log_agg.global_rpc_pause_until and log_agg.global_rpc_pause_until > now:
        global_until = log_agg.global_rpc_pause_until.isoformat()

    return {
        "per_mint_pause": per_mint_pause,
        "per_mint_cooldown": per_mint_cooldown,
        "global_trading_paused_until": global_until,
        "lane_cooldowns": [],  # reserved
    }


# ---- Main pipeline ----


def run_monitoring(
    state_path: Path,
    log_path: Path,
    status_path: Optional[Path] = None,
    wallet_id_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Read-only pipeline: load state and log, produce monitoring summary.
    See docs/trading/mint-ladder-bot-monitoring-runtime.md §6 output shape.
    """
    state, state_err = load_state(state_path)
    log_agg, log_err = parse_log(log_path)

    wallet_id = wallet_id_override or state.get("wallet")
    if not wallet_id and status_path and status_path.exists():
        try:
            status_data = json.loads(status_path.read_text())
            if isinstance(status_data.get("wallet"), str):
                wallet_id = status_data["wallet"]
        except (json.JSONDecodeError, OSError):
            pass
    if not wallet_id:
        wallet_id = "unknown"

    wallet_exposure = build_wallet_exposure(state, log_agg, wallet_id_override=wallet_id)
    failures_and_abnormal = build_failures_and_abnormal(state, log_agg)
    cooldowns = build_cooldowns(state, log_agg)

    summary = {
        "inputs_used": {
            "state_path": str(state_path),
            "log_path": str(log_path),
            "status_path": str(status_path) if status_path else None,
        },
        "warnings": [e for e in (state_err, log_err) if e],
        "wallet_id": wallet_id,
        "wallet_exposure": wallet_exposure,
        "failures_and_abnormal": failures_and_abnormal,
        "cooldowns": cooldowns,
    }

    if log_agg.last_cycle_summary:
        summary["last_cycle_summary"] = log_agg.last_cycle_summary
    if log_agg.last_run_totals:
        summary["last_run_totals"] = log_agg.last_run_totals

    return summary
