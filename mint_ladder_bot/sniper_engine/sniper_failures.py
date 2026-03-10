"""
Sniper failure tracking — runtime/stats/sniper_failures.json.

Stores failure events with reason codes and summary: failed_buys_last_hour, failed_buys_total.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REASON_TX_ERROR = "tx_error"
REASON_ROUTE_MISSING = "route_missing"
REASON_INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"
REASON_RPC_FAILURE = "rpc_failure"
REASON_CONFIRM_FILL_FAILED = "confirm_fill_failed"
REASON_SEND_FAILED = "send_failed"
REASON_BUILD_SWAP_FAILED = "build_swap_failed"
REASON_SIGN_FAILED = "sign_failed"
REASON_OTHER = "other"


def _default_stats() -> Dict[str, Any]:
    return {
        "failed_buys_total": 0,
        "failed_buys_last_hour": 0,
        "failures": [],
        "updated_at": None,
    }


def load_failures(path: Path) -> Dict[str, Any]:
    if not path or not path.exists():
        return _default_stats()
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
        return _default_stats()
    except Exception as e:
        logger.warning("sniper_failures load failed %s: %s", path, e)
        return _default_stats()


def save_failures(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
    # Keep last 200 failures only
    failures = data.get("failures") or []
    if len(failures) > 200:
        data["failures"] = failures[-200:]
    path.write_text(json.dumps(data, indent=2))


def record_failure(
    path: Path,
    mint: str,
    reason: str,
    tx_error: Optional[str] = None,
    route_missing: bool = False,
    insufficient_liquidity: bool = False,
    rpc_failure: bool = False,
) -> None:
    data = load_failures(path)
    data["failed_buys_total"] = data.get("failed_buys_total", 0) + 1
    data.setdefault("failures", []).append({
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "mint": mint[:12],
        "reason": reason,
        "tx_error": tx_error[:200] if tx_error else None,
        "route_missing": route_missing,
        "insufficient_liquidity": insufficient_liquidity,
        "rpc_failure": rpc_failure,
    })
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
    data["failed_buys_last_hour"] = sum(1 for f in data.get("failures") or [] if (f.get("ts") or "") >= cutoff)
    save_failures(path, data)


def get_summary(path: Path) -> Dict[str, Any]:
    data = load_failures(path)
    return {
        "failed_buys_total": data.get("failed_buys_total", 0),
        "failed_buys_last_hour": data.get("failed_buys_last_hour", 0),
        "updated_at": data.get("updated_at"),
    }
