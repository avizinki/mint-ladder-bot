"""
Deployer reputation tracking — runtime/reputation/deployer_history.json.

Tracks per deployer: tokens_created, rejected_tokens, successful_trades.
Used by token filter for deployer_ok score. No secrets; local file only.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _default_history() -> Dict[str, Any]:
    return {"deployers": {}, "updated_at": None}


def load_deployer_history(path: Path) -> Dict[str, Any]:
    """Load deployer_history.json. Returns { deployers: { wallet: { tokens_created, rejected_tokens, successful_trades } }, updated_at }."""
    if not path or not path.exists():
        return _default_history()
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "deployers" in data:
            return data
        return _default_history()
    except Exception as e:
        logger.warning("deployer_history load failed %s: %s", path, e)
        return _default_history()


def save_deployer_history(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    data["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
    path.write_text(json.dumps(data, indent=2))


def record_token_created(path: Path, deployer_wallet: str) -> None:
    data = load_deployer_history(path)
    data.setdefault("deployers", {})
    data["deployers"].setdefault(deployer_wallet, {"tokens_created": 0, "rejected_tokens": 0, "successful_trades": 0})
    data["deployers"][deployer_wallet]["tokens_created"] = data["deployers"][deployer_wallet].get("tokens_created", 0) + 1
    save_deployer_history(path, data)


def record_rejected(path: Path, deployer_wallet: str) -> None:
    data = load_deployer_history(path)
    data.setdefault("deployers", {})
    data["deployers"].setdefault(deployer_wallet, {"tokens_created": 0, "rejected_tokens": 0, "successful_trades": 0})
    data["deployers"][deployer_wallet]["rejected_tokens"] = data["deployers"][deployer_wallet].get("rejected_tokens", 0) + 1
    save_deployer_history(path, data)


def record_successful_trade(path: Path, deployer_wallet: str) -> None:
    data = load_deployer_history(path)
    data.setdefault("deployers", {})
    data["deployers"].setdefault(deployer_wallet, {"tokens_created": 0, "rejected_tokens": 0, "successful_trades": 0})
    data["deployers"][deployer_wallet]["successful_trades"] = data["deployers"][deployer_wallet].get("successful_trades", 0) + 1
    save_deployer_history(path, data)


def get_deployer_stats(path: Path, deployer_wallet: str) -> Dict[str, int]:
    """Return { tokens_created, rejected_tokens, successful_trades } for deployer."""
    data = load_deployer_history(path)
    d = data.get("deployers", {}).get(deployer_wallet, {})
    return {
        "tokens_created": int(d.get("tokens_created", 0)),
        "rejected_tokens": int(d.get("rejected_tokens", 0)),
        "successful_trades": int(d.get("successful_trades", 0)),
    }


def is_deployer_acceptable(path: Optional[Path], deployer_wallet: str, max_rejected_ratio: float = 0.95) -> bool:
    """
    True if we have no history or reject ratio is below max. If no path, returns True (no data).
    """
    if not path or not deployer_wallet:
        return True
    stats = get_deployer_stats(path, deployer_wallet)
    created = stats["tokens_created"]
    rejected = stats["rejected_tokens"]
    if created == 0:
        return True
    return (rejected / created) < max_rejected_ratio
