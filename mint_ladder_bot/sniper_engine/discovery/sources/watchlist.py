"""
Watchlist discovery source adapter.

Reads DISCOVERY_WATCHLIST_PATH (JSON or YAML file) for manually curated mints.
File format (JSON array):
  ["MintAddress1", "MintAddress2", ...]
or with metadata (JSON array of objects):
  [{"mint": "MintAddress1", "symbol": "TOKEN", "note": "reason"}, ...]

source_id: "watchlist"
source_confidence: 0.8 (operator-curated; high trust)

Returns empty list when path not configured or file missing/invalid.
Reads the file fresh each cycle so operators can update without restarting.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..model import DiscoveredCandidate, make_candidate
from ..registry import register_source

logger = logging.getLogger(__name__)

SOURCE_ID = "watchlist"
SOURCE_CONFIDENCE = 0.8


def _load_watchlist(path: str) -> List[Dict[str, Any]]:
    """Load and normalize watchlist file. Returns [] on any error."""
    p = Path(path)
    if not p.exists():
        logger.debug("watchlist path not found: %s", path)
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("watchlist read error: %s", e)
        return []

    # Try JSON first, then YAML if available
    data: Any = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # optional dependency
            data = yaml.safe_load(text)
        except Exception:
            logger.warning("watchlist parse failed (not valid JSON or YAML): %s", path)
            return []

    if not isinstance(data, list):
        logger.warning("watchlist file must be a JSON/YAML array: %s", path)
        return []

    items: List[Dict[str, Any]] = []
    for entry in data:
        if isinstance(entry, str):
            items.append({"mint": entry.strip()})
        elif isinstance(entry, dict):
            items.append(entry)
    return items


def _watchlist_path() -> Optional[str]:
    return (os.getenv("DISCOVERY_WATCHLIST_PATH") or "").strip() or None


def _fetch(limit: int) -> List[DiscoveredCandidate]:
    path = _watchlist_path()
    if not path:
        return []
    items = _load_watchlist(path)
    now = datetime.now(tz=timezone.utc)
    out: List[DiscoveredCandidate] = []
    for item in items[:limit]:
        mint = (item.get("mint") or "").strip()
        if not mint or len(mint) < 32:
            continue
        symbol = (item.get("symbol") or "").strip() or None
        note = item.get("note")
        metadata: Dict[str, Any] = {}
        if note:
            metadata["note"] = note
        out.append(make_candidate(
            mint=mint,
            source_id=SOURCE_ID,
            source_confidence=SOURCE_CONFIDENCE,
            symbol=symbol,
            metadata=metadata,
            discovered_at=now,
        ))
    return out


def register() -> None:
    register_source(SOURCE_ID, _fetch)
