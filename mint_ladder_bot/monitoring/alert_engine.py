"""
Alert engine for mint-ladder-bot: run monitoring and evaluate conditions into alert dicts.

Read-only: calls runtime_monitor.run_monitoring(); no writes to state or log.
No delivery (no email, webhook); alerts are returned or passed to an optional callback.

Spec: docs/trading/mint-ladder-bot-monitor-alerts.md
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .runtime_monitor import run_monitoring

# Keys that may contain sensitive data; redacted when writing to output_path.
_SECRET_KEYS = frozenset({"last_error"})


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _alert_for_file(alert: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of alert safe for file output (no secrets)."""
    out = dict(alert)
    for k in _SECRET_KEYS:
        if k in out:
            out[k] = "[redacted]"
    return out


def _emit(
    alerts: List[Dict[str, Any]],
    alert: Dict[str, Any],
    callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    output_path: Optional[Path] = None,
) -> None:
    """Append alert with timestamp, optionally invoke callback, optionally append one JSON line to output_path."""
    alert = dict(alert)
    if "timestamp" not in alert:
        alert["timestamp"] = _now_utc().isoformat()
    alerts.append(alert)
    if callback is not None:
        callback(alert)
    if output_path is not None:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_alert_for_file(alert), default=str) + "\n")


def run_alerts(
    state_path: Path,
    log_path: Path,
    status_path: Optional[Path] = None,
    *,
    wallet_id_override: Optional[str] = None,
    lane_cooldowns: Optional[List[Dict[str, Any]]] = None,
    failure_count_threshold: int = 3,
    wallet_exposure_failure_threshold: Optional[int] = 5,
    wallet_exposure_paused_threshold: Optional[int] = None,
    callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    output_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """
    Run monitoring (read-only), evaluate conditions, produce list of alert dicts.

    Conditions: RPC failures/global pause, wallet exposure (high failure count or
    many paused mints), lane cooldown (from monitoring or lane_cooldowns),
    STOP file (from log), abnormal execution (repeated failures, liquidity_collapse).

    Alert dict keys: alert_type, severity (optional), message, wallet_id/lane_id/mint
    if applicable, timestamp. No delivery; output is return value, optional callback,
    and optional output_path (when set, each alert is appended as one JSON line; no secrets).
    Read-only: no writes to state or log (except optional output_path).
    """
    alerts: List[Dict[str, Any]] = []
    summary = run_monitoring(
        state_path, log_path, status_path, wallet_id_override=wallet_id_override
    )
    wallet_id = summary.get("wallet_id") or "unknown"
    exp = summary.get("wallet_exposure") or {}
    failures_abnormal = summary.get("failures_and_abnormal") or []
    cooldowns = summary.get("cooldowns") or {}

    # ---- RPC failures / global pause ----
    for ev in failures_abnormal:
        if ev.get("condition") == "global_rpc_pause":
            _emit(
                alerts,
                {
                    "alert_type": "rpc_global_pause",
                    "severity": "high",
                    "message": "Global trading paused due to RPC instability",
                    "paused_until": ev.get("paused_until"),
                    "rpc_failures_count": ev.get("rpc_failures_count"),
                    "wallet_id": wallet_id,
                },
                callback,
                output_path,
            )
            break

    # ---- STOP file ----
    for ev in failures_abnormal:
        if ev.get("condition") == "stop_file_active":
            _emit(
                alerts,
                {
                    "alert_type": "stop_file",
                    "severity": "high",
                    "message": "STOP file present; trading disabled",
                    "wallet_id": wallet_id,
                },
                callback,
                output_path,
            )
            break

    # ---- Wallet exposure (high failure count or many paused mints) ----
    failure_total = exp.get("failure_count_total", 0)
    mints_paused = exp.get("mints_paused", 0)
    if wallet_exposure_failure_threshold is not None and failure_total >= wallet_exposure_failure_threshold:
        _emit(
            alerts,
            {
                "alert_type": "wallet_exposure",
                "severity": "medium",
                "message": f"Wallet failure count high: {failure_total}",
                "wallet_id": wallet_id,
                "failure_count_total": failure_total,
            },
            callback,
            output_path,
        )
    if wallet_exposure_paused_threshold is not None and mints_paused >= wallet_exposure_paused_threshold:
        _emit(
            alerts,
            {
                "alert_type": "wallet_exposure",
                "severity": "medium",
                "message": f"Many mints paused: {mints_paused}",
                "wallet_id": wallet_id,
                "mints_paused": mints_paused,
            },
            callback,
            output_path,
        )

    # ---- Lane cooldown (from monitoring or passed in) ----
    lane_list = cooldowns.get("lane_cooldowns") or []
    if lane_cooldowns is not None:
        lane_list = lane_cooldowns
    for lane in lane_list:
        if isinstance(lane, dict):
            _emit(
                alerts,
                {
                    "alert_type": "lane_cooldown",
                    "severity": "low",
                    "message": "Lane cooldown active",
                    "lane_id": lane.get("lane_id"),
                    "wallet_id": wallet_id,
                },
                callback,
                output_path,
            )

    # ---- Abnormal execution: per-mint repeated failures, liquidity_collapse ----
    for ev in failures_abnormal:
        cond = ev.get("condition")
        mint_id = ev.get("mint")
        if cond == "per_mint_repeated_failures":
            _emit(
                alerts,
                {
                    "alert_type": "per_mint_repeated_failures",
                    "severity": "medium",
                    "message": f"Mint {mint_id} repeated failures or paused",
                    "mint": mint_id,
                    "wallet_id": wallet_id,
                    "failure_count": ev.get("failure_count"),
                    "paused_until": ev.get("paused_until"),
                    "last_error": ev.get("last_error"),
                },
                callback,
                output_path,
            )
        elif cond == "liquidity_collapse":
            _emit(
                alerts,
                {
                    "alert_type": "liquidity_collapse",
                    "severity": "high",
                    "message": f"Liquidity collapse for mint {mint_id or 'unknown'}",
                    "mint": mint_id,
                    "wallet_id": wallet_id,
                    "paused_until": ev.get("paused_until"),
                    "source": ev.get("source"),
                },
                callback,
                output_path,
            )
        elif cond == "confirm_uncertain":
            _emit(
                alerts,
                {
                    "alert_type": "confirm_uncertain",
                    "severity": "low",
                    "message": f"Mint {ev.get('mint')} confirm uncertain; paused",
                    "mint": ev.get("mint"),
                    "wallet_id": wallet_id,
                    "step_id": ev.get("step_id"),
                    "pause_min": ev.get("pause_min"),
                },
                callback,
                output_path,
            )
        elif cond == "startup_validation_failed":
            _emit(
                alerts,
                {
                    "alert_type": "startup_validation_failed",
                    "severity": "medium",
                    "message": f"Mint {ev.get('mint')} startup validation failed",
                    "mint": ev.get("mint"),
                    "wallet_id": wallet_id,
                    "pause_min": ev.get("pause_min"),
                },
                callback,
                output_path,
            )

    return alerts
