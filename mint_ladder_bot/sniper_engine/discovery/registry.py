"""
Discovery source registry.

Defines the SourceAdapter protocol and provides a registry of all built-in adapters.
The pipeline queries the registry to fetch candidates from enabled sources only.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from .model import DiscoveredCandidate

logger = logging.getLogger(__name__)

# Adapter callable: (limit: int) -> List[DiscoveredCandidate]
SourceAdapter = Callable[[int], List[DiscoveredCandidate]]

# Global source registry: source_id -> adapter callable
_REGISTRY: Dict[str, SourceAdapter] = {}


def register_source(source_id: str, adapter: SourceAdapter) -> None:
    """Register a source adapter. Safe to call at import time."""
    _REGISTRY[source_id] = adapter


def get_registered_source_ids() -> List[str]:
    return list(_REGISTRY.keys())


def fetch_from_sources(
    source_allowlist: Optional[List[str]],
    limit_per_source: int = 50,
) -> List[DiscoveredCandidate]:
    """
    Fetch candidates from all registered sources that pass the allowlist.

    source_allowlist: None or [] = all sources; non-empty = only listed source_ids.
    Each source is called independently; failures are logged and skipped.
    """
    allowed = set(source_allowlist) if source_allowlist else None
    out: List[DiscoveredCandidate] = []
    for source_id, adapter in list(_REGISTRY.items()):
        if allowed is not None and source_id not in allowed:
            continue
        try:
            candidates = adapter(limit_per_source)
            out.extend(candidates)
            logger.debug("discovery source=%s returned %d candidates", source_id, len(candidates))
        except Exception as e:
            logger.warning("discovery source=%s failed: %s", source_id, str(e)[:200])
    return out
