"""
Discovery source adapters.

Each adapter module exposes a register() function that calls registry.register_source().
Call register_all() to activate all built-in sources.
"""
from __future__ import annotations


def register_all() -> None:
    """Register all built-in source adapters into the global registry."""
    from . import pumpfun_adapter, test_source, watchlist, whale_copy_adapter
    pumpfun_adapter.register()
    test_source.register()
    watchlist.register()
    whale_copy_adapter.register()
