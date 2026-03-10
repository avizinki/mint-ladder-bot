"""
Pump.fun launch source — real detection of new token launches.

Polls configurable API (e.g. PUMPFUN_NEW_TOKENS_URL). Normalizes to LaunchCandidate
with bonding_curve, deployer, liquidity, etc. Deduplication via recent_mints cache.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import httpx

from .launch_detector import LaunchCandidate

logger = logging.getLogger(__name__)

def _pumpfun_url() -> Optional[str]:
    return (os.getenv("PUMPFUN_NEW_TOKENS_URL") or "").strip() or None

def _pumpfun_poll_interval_sec() -> float:
    try:
        return max(5.0, float(os.getenv("PUMPFUN_POLL_INTERVAL_SECONDS", "30").strip()))
    except ValueError:
        return 30.0

PUMPFUN_API_KEY_ENV = "PUMPFUN_API_KEY"
REQUEST_TIMEOUT_S = 15.0
RECENT_MINTS_MAX = 500
RECENT_MINTS_TTL_SEC = 600  # 10 min

# Deduplication: mint -> last_seen_timestamp (monotonic)
_recent_mints: Dict[str, float] = {}
_last_fetch_time: float = 0.0


def _parse_ts(value: Any) -> datetime:
    """Parse timestamp from API (unix s or ms) to UTC datetime."""
    now = datetime.now(tz=timezone.utc)
    if value is None:
        return now
    try:
        t = float(value)
        if t > 1e12:
            t = t / 1000.0
        return datetime.fromtimestamp(t, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return now


def _normalize_item(item: Dict[str, Any]) -> Optional[LaunchCandidate]:
    """Map one API item to LaunchCandidate. Extracts bonding_curve, initial_liquidity, deployer, name, symbol, created ts."""
    mint = (item.get("mint") or item.get("address") or item.get("token_address") or "").strip()
    if not mint or len(mint) < 32:
        return None
    ts = _parse_ts(
        item.get("created_timestamp") or item.get("created_at") or item.get("timestamp") or item.get("time")
    )
    deployer = (item.get("deployer") or item.get("user") or item.get("creator") or "").strip()
    if isinstance(deployer, dict):
        deployer = deployer.get("address") or deployer.get("pubkey") or ""
    name = (item.get("name") or "").strip() or None
    symbol = (item.get("symbol") or "").strip() or None
    bonding_curve = (item.get("bonding_curve") or item.get("bonding_curve_address") or item.get("curve") or "").strip()
    if isinstance(bonding_curve, dict):
        bonding_curve = bonding_curve.get("address") or ""
    initial_liquidity = None
    if isinstance(item.get("initial_liquidity"), (int, float)):
        initial_liquidity = float(item["initial_liquidity"])
    if initial_liquidity is None and isinstance(item.get("liquidity_sol"), (int, float)):
        initial_liquidity = float(item["liquidity_sol"])
    liquidity_usd = None
    if isinstance(item.get("liquidity_usd"), (int, float)):
        liquidity_usd = float(item["liquidity_usd"])
    if liquidity_usd is None and isinstance(item.get("liquidity"), (int, float)):
        liquidity_usd = float(item["liquidity"])
    metadata: Dict[str, Any] = {}
    if deployer:
        metadata["deployer"] = deployer
    if name:
        metadata["name"] = name
    if symbol:
        metadata["symbol"] = symbol
    if liquidity_usd is not None:
        metadata["liquidity_usd"] = liquidity_usd
    if bonding_curve:
        metadata["bonding_curve_address"] = bonding_curve
    if initial_liquidity is not None:
        metadata["initial_liquidity"] = initial_liquidity
    metadata["created_timestamp"] = ts.isoformat()
    return LaunchCandidate(
        mint=mint,
        source="pumpfun",
        detected_at=ts,
        metadata=metadata if metadata else None,
        raw={k: v for k, v in item.items() if k in ("mint", "created_timestamp", "bonding_progress", "deployer", "bonding_curve")},
    )


def _recent_mints_dedupe(mint: str) -> bool:
    """Return True if mint was seen recently; then record. Caller should skip if True."""
    global _recent_mints, _last_fetch_time
    now = time.monotonic()
    # Prune old entries
    cutoff = now - RECENT_MINTS_TTL_SEC
    _recent_mints = {m: t for m, t in _recent_mints.items() if t > cutoff}
    if len(_recent_mints) > RECENT_MINTS_MAX:
        by_time = sorted(_recent_mints.items(), key=lambda x: x[1])
        _recent_mints = dict(by_time[-RECENT_MINTS_MAX:])
    if mint in _recent_mints:
        return True
    _recent_mints[mint] = now
    return False


def fetch_pumpfun_launches(
    limit: int = 50,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    skip_recent_cache: bool = False,
) -> List[LaunchCandidate]:
    """
    Poll pump.fun–style API for new token launches. Deduplicates via recent_mints cache.
    Expects JSON array or { data: [...] } with items containing at least mint/address.
    """
    url = (api_url or _pumpfun_url() or "").strip()
    if not url:
        return []
    key = (api_key or os.getenv(PUMPFUN_API_KEY_ENV) or "").strip()
    headers: Dict[str, str] = {}
    if key:
        headers["x-api-key"] = key
        headers["Authorization"] = f"Bearer {key}"
    try:
        resp = httpx.get(url, headers=headers or None, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("pumpfun fetch failed url=%s: %s", url[:50], str(e)[:200])
        return []

    items: List[Dict[str, Any]] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("data") or data.get("tokens") or data.get("coins") or data.get("results") or []
    if not isinstance(items, list):
        return []

    out: List[LaunchCandidate] = []
    for i, item in enumerate(items[:limit]):
        if not isinstance(item, dict):
            continue
        c = _normalize_item(item)
        if c is not None:
            if not skip_recent_cache and _recent_mints_dedupe(c.mint):
                continue
            out.append(c)
            meta = c.metadata or {}
            logger.info(
                "LAUNCH_DETECTED mint=%s source=pumpfun deployer=%s liquidity_usd=%s",
                c.mint[:12],
                (meta.get("deployer") or "n/a")[:8],
                meta.get("liquidity_usd"),
            )
    return out


def detect_pumpfun(limit: int = 50) -> List[LaunchCandidate]:
    """
    Detect new tokens from pump.fun. Uses PUMPFUN_NEW_TOKENS_URL and optional PUMPFUN_API_KEY.
    Returns list of LaunchCandidate; empty if URL not set or request fails.
    """
    return fetch_pumpfun_launches(limit=limit)
