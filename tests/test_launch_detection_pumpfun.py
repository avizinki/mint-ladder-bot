"""Tests for pump.fun launch detection: parsing, dedupe, metadata."""
from __future__ import annotations

import os
from unittest.mock import patch

from mint_ladder_bot.sniper_engine.pumpfun_source import (
    _normalize_item,
    _recent_mints_dedupe,
    fetch_pumpfun_launches,
    RECENT_MINTS_TTL_SEC,
)
from mint_ladder_bot.sniper_engine.launch_detector import LaunchCandidate


def test_normalize_item_full_metadata():
    item = {
        "mint": "So11111111111111111111111111111111111111112",
        "created_timestamp": 1700000000,
        "deployer": "DeployerWallet111111111111111111111111111",
        "name": "Test Token",
        "symbol": "TST",
        "liquidity_usd": 10000.0,
        "bonding_curve_address": "Curve111111111111111111111111111111111111111",
        "initial_liquidity": 50.0,
    }
    c = _normalize_item(item)
    assert c is not None
    assert c.mint == item["mint"]
    assert c.source == "pumpfun"
    assert c.metadata is not None
    assert c.metadata.get("deployer") == item["deployer"]
    assert c.metadata.get("name") == item["name"]
    assert c.metadata.get("symbol") == item["symbol"]
    assert c.metadata.get("liquidity_usd") == 10000.0
    assert c.metadata.get("bonding_curve_address") == item["bonding_curve_address"]
    assert c.metadata.get("initial_liquidity") == 50.0
    assert "created_timestamp" in c.metadata


def test_normalize_item_minimal():
    item = {"mint": "MintAddress1111111111111111111111111111111111111111"}
    c = _normalize_item(item)
    assert c is not None
    assert c.mint == item["mint"]
    assert c.source == "pumpfun"


def test_normalize_item_missing_mint():
    assert _normalize_item({}) is None
    assert _normalize_item({"address": ""}) is None


def test_recent_mints_dedupe():
    mint = "TestMint1111111111111111111111111111111111111111"
    assert _recent_mints_dedupe(mint) is False  # first time
    assert _recent_mints_dedupe(mint) is True   # second time same mint


def test_fetch_pumpfun_launches_empty_without_url():
    with patch.dict(os.environ, {}, clear=False):
        out = fetch_pumpfun_launches(limit=5)
    assert out == []
