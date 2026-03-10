"""
Launch detection: new token candidates from pump.fun, Raydium, Meteora, Jupiter.

Output: stream or list of LaunchCandidate for token_filter and sniper decision.
Solana ecosystem only.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Placeholder: real implementations will call pump.fun API, Raydium/Meteora program logs, Jupiter route discovery.


@dataclass
class LaunchCandidate:
    """A newly detected token launch candidate."""

    mint: str
    source: str  # "pump_fun" | "raydium" | "meteora" | "jupiter" | "test"
    detected_at: datetime
    metadata: Optional[Dict[str, Any]] = None  # symbol, name, pool address, etc.
    raw: Optional[Dict[str, Any]] = None  # raw API/event payload for debugging


def _detect_test_mints(limit: int = 50) -> List[LaunchCandidate]:
    """
    Config-driven test source: SNIPER_TEST_MINTS (comma-separated mint addresses).
    Returns one LaunchCandidate per mint with source="test" for pipeline testing.
    """
    raw = (os.getenv("SNIPER_TEST_MINTS") or "").strip()
    if not raw:
        return []
    mints = [m.strip() for m in raw.split(",") if m.strip()][:limit]
    now = datetime.now(tz=timezone.utc)
    out: List[LaunchCandidate] = []
    for mint in mints:
        c = LaunchCandidate(mint=mint, source="test", detected_at=now, metadata={"symbol": None, "name": None})
        out.append(c)
        logger.debug("LAUNCH_DETECTED mint=%s source=test", mint[:12])
    return out


def detect_pump_fun(limit: int = 50) -> List[LaunchCandidate]:
    """
    Detect new tokens from pump.fun. Uses pumpfun_source when PUMPFUN_NEW_TOKENS_URL is set.
    """
    try:
        from .pumpfun_source import detect_pumpfun
        return detect_pumpfun(limit=limit)
    except Exception as e:
        logger.debug("detect_pump_fun failed: %s", e)
        return []


def detect_raydium(limit: int = 50) -> List[LaunchCandidate]:
    """
    Detect new pools from Raydium (CLMM / AMM).
    Stub: returns empty list. Implement via Raydium API or pool creation logs.
    """
    # TODO: Raydium new pool events or API
    return []


def detect_meteora(limit: int = 50) -> List[LaunchCandidate]:
    """
    Detect new pools from Meteora.
    Stub: returns empty list. Implement via Meteora API or pool creation logs.
    """
    # TODO: Meteora DLMM / pool creation
    return []


def detect_jupiter_routes(limit: int = 50) -> List[LaunchCandidate]:
    """
    Discover new mints via Jupiter route discovery (new mint in route set).
    Stub: returns empty list. Implement via Jupiter token list / route API.
    """
    # TODO: Jupiter token list diff or route discovery for new mints
    return []


def detect_all(limit_per_source: int = 20) -> List[LaunchCandidate]:
    """Run all detectors and return combined list (deduped by mint). Test source runs first if SNIPER_TEST_MINTS set."""
    seen: set[str] = set()
    out: List[LaunchCandidate] = []
    # Test source first (config-driven)
    for c in _detect_test_mints(limit=limit_per_source):
        if c.mint not in seen:
            seen.add(c.mint)
            out.append(c)
    for fn in (detect_pump_fun, detect_raydium, detect_meteora, detect_jupiter_routes):
        for c in fn(limit=limit_per_source):
            if c.mint not in seen:
                seen.add(c.mint)
                out.append(c)
    return out
