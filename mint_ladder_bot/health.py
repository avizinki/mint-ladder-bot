"""
Runtime health status for dashboard and watchdog.
Writes health_status.json so dashboard never fails with "not found or unreadable".
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .runtime_paths import get_health_status_path

logger = logging.getLogger(__name__)


def write_health_status(
    data_dir: Path,  # kept for backwards-compat signature; ignored for path resolution
    state: Any,
    runtime_info: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Write health_status.json to data_dir. Never raises; logs on write failure.
    runtime_info: optional dict with keys cycles, rpc_latency_ms, errors (list), paused_mints,
    loop_heartbeat_at, last_successful_cycle_at, last_failed_cycle_at, current_cycle_number,
    config_profile, clean_start_active, backfill_completed, rpc_provider_label, buyback_enabled.
    """
    data_dir = Path(data_dir).resolve()
    info = runtime_info or {}
    cycles = info.get("cycles", 0)
    rpc_latency_ms = info.get("rpc_latency_ms")
    if rpc_latency_ms is None:
        rpc_latency_ms = 0.0
    errors: List[str] = info.get("errors") or []
    if not isinstance(errors, list):
        errors = []
    paused_mints = info.get("paused_mints", 0)
    wallet = getattr(state, "wallet", None) if state else None
    if isinstance(wallet, str):
        pass
    else:
        wallet = str(wallet) if wallet else ""

    now_iso = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    payload: Dict[str, Any] = {
        "ok": True,
        "timestamp": now_iso,
        "cycles": cycles,
        "wallet": wallet,
        "rpc_latency_ms": rpc_latency_ms,
        "errors": errors,
        "paused_mints": paused_mints,
        "loop_heartbeat_at": info.get("loop_heartbeat_at", now_iso),
        "last_successful_cycle_at": info.get("last_successful_cycle_at"),
        "last_failed_cycle_at": info.get("last_failed_cycle_at"),
        "current_cycle_number": info.get("current_cycle_number", cycles),
        "config_profile": info.get("config_profile"),
        "clean_start_active": info.get("clean_start_active"),
        "backfill_completed": info.get("backfill_completed"),
        "rpc_provider_label": info.get("rpc_provider_label"),
        "buyback_enabled": info.get("buyback_enabled"),
    }
    if info.get("sell_readiness") is not None:
        payload["sell_readiness"] = info["sell_readiness"]
    if info.get("runner_mode") is not None:
        payload["runner_mode"] = info["runner_mode"]
    if info.get("trading_disabled") is not None:
        payload["trading_disabled"] = info["trading_disabled"]
    if info.get("swap_provider") is not None:
        payload["swap_provider"] = info["swap_provider"]
    # Canonical health path under centralized runtime tree.
    path = get_health_status_path()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("Failed to write health_status.json: %s", e)
