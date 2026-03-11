"""
Source adapter normalization tests.

Each adapter must produce well-formed DiscoveredCandidate objects with:
- non-empty mint
- valid source_id
- source_confidence in [0.0, 1.0]
- discovered_at is timezone-aware
"""
from __future__ import annotations

import os
import json
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from mint_ladder_bot.sniper_engine.discovery.model import DiscoveredCandidate


# ---------------------------------------------------------------------------
# Pumpfun adapter
# ---------------------------------------------------------------------------

def test_pumpfun_adapter_empty_when_no_url(monkeypatch) -> None:
    monkeypatch.delenv("PUMPFUN_NEW_TOKENS_URL", raising=False)
    from mint_ladder_bot.sniper_engine.discovery.sources.pumpfun_adapter import _fetch
    result = _fetch(10)
    assert result == []


def test_pumpfun_adapter_normalizes_launch_candidate() -> None:
    """Adapter converts LaunchCandidate → DiscoveredCandidate with correct fields."""
    from mint_ladder_bot.sniper_engine.launch_detector import LaunchCandidate
    from mint_ladder_bot.sniper_engine.discovery.sources.pumpfun_adapter import SOURCE_ID, SOURCE_CONFIDENCE
    from mint_ladder_bot.sniper_engine.discovery.model import make_candidate

    fake_mint = "A" * 44
    now = datetime.now(tz=timezone.utc)
    lc = LaunchCandidate(
        mint=fake_mint,
        source="pumpfun",
        detected_at=now,
        metadata={"symbol": "FAKE", "liquidity_usd": 12345.0, "deployer": "D" * 44},
    )

    # Simulate what the adapter does
    meta = lc.metadata or {}
    cand = make_candidate(
        mint=lc.mint,
        source_id=SOURCE_ID,
        source_confidence=SOURCE_CONFIDENCE,
        symbol=meta.get("symbol"),
        liquidity_usd=meta.get("liquidity_usd"),
        deployer=meta.get("deployer"),
        metadata=dict(meta),
        discovered_at=lc.detected_at,
    )

    assert cand.mint == fake_mint
    assert cand.source_id == "pumpfun"
    assert 0.0 <= cand.source_confidence <= 1.0
    assert cand.symbol == "FAKE"
    assert cand.liquidity_usd == 12345.0
    assert cand.deployer == "D" * 44
    assert cand.discovered_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Test source adapter
# ---------------------------------------------------------------------------

def test_test_source_empty_when_no_env(monkeypatch) -> None:
    monkeypatch.delenv("SNIPER_TEST_MINTS", raising=False)
    from mint_ladder_bot.sniper_engine.discovery.sources.test_source import _fetch
    result = _fetch(10)
    assert result == []


def test_test_source_returns_candidates(monkeypatch) -> None:
    fake_mint_a = "A" * 44
    fake_mint_b = "B" * 44
    monkeypatch.setenv("SNIPER_TEST_MINTS", f"{fake_mint_a},{fake_mint_b}")
    from mint_ladder_bot.sniper_engine.discovery.sources import test_source
    # Reload to pick up env
    import importlib
    importlib.reload(test_source)
    from mint_ladder_bot.sniper_engine.discovery.sources.test_source import _fetch

    result = _fetch(10)
    mints = [c.mint for c in result]
    assert fake_mint_a in mints
    assert fake_mint_b in mints
    for c in result:
        assert c.source_id == "test"
        assert c.source_confidence == 1.0
        assert c.discovered_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Watchlist adapter
# ---------------------------------------------------------------------------

def test_watchlist_empty_when_no_path(monkeypatch) -> None:
    monkeypatch.delenv("DISCOVERY_WATCHLIST_PATH", raising=False)
    from mint_ladder_bot.sniper_engine.discovery.sources.watchlist import _fetch
    result = _fetch(10)
    assert result == []


def test_watchlist_loads_json_string_array(tmp_path) -> None:
    fake_mint = "W" * 44
    wl = tmp_path / "watchlist.json"
    wl.write_text(json.dumps([fake_mint]))

    from mint_ladder_bot.sniper_engine.discovery.sources.watchlist import _fetch, _watchlist_path
    with patch.object(
        __import__("mint_ladder_bot.sniper_engine.discovery.sources.watchlist", fromlist=["_watchlist_path"]),
        "_watchlist_path",
        return_value=str(wl),
    ):
        # Direct test via _load_watchlist
        from mint_ladder_bot.sniper_engine.discovery.sources.watchlist import _load_watchlist
        items = _load_watchlist(str(wl))
    assert len(items) == 1
    assert items[0]["mint"] == fake_mint


def test_watchlist_loads_json_object_array(tmp_path) -> None:
    fake_mint = "W" * 44
    wl = tmp_path / "watchlist.json"
    wl.write_text(json.dumps([{"mint": fake_mint, "symbol": "WATCH", "note": "test"}]))

    from mint_ladder_bot.sniper_engine.discovery.sources.watchlist import _load_watchlist
    items = _load_watchlist(str(wl))
    assert len(items) == 1
    assert items[0]["mint"] == fake_mint
    assert items[0]["symbol"] == "WATCH"


def test_watchlist_skips_short_mints(tmp_path) -> None:
    wl = tmp_path / "watchlist.json"
    wl.write_text(json.dumps(["short"]))  # too short

    from mint_ladder_bot.sniper_engine.discovery.sources.watchlist import _load_watchlist, SOURCE_ID, SOURCE_CONFIDENCE
    from mint_ladder_bot.sniper_engine.discovery.sources.watchlist import _fetch as wl_fetch

    items = _load_watchlist(str(wl))
    assert items == [{"mint": "short"}]

    # _fetch filters out mints with len < 32
    import os
    os.environ["DISCOVERY_WATCHLIST_PATH"] = str(wl)
    try:
        result = wl_fetch(10)
        assert result == []
    finally:
        del os.environ["DISCOVERY_WATCHLIST_PATH"]


def test_watchlist_missing_file_returns_empty() -> None:
    from mint_ladder_bot.sniper_engine.discovery.sources.watchlist import _load_watchlist
    items = _load_watchlist("/nonexistent/path/watchlist.json")
    assert items == []


def test_watchlist_invalid_json_returns_empty(tmp_path) -> None:
    wl = tmp_path / "watchlist.json"
    wl.write_text("not json and not yaml !!!{{}")
    from mint_ladder_bot.sniper_engine.discovery.sources.watchlist import _load_watchlist
    items = _load_watchlist(str(wl))
    assert items == []
