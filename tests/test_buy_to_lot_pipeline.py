"""Tests for buy → lot → ladder pipeline (simulated and integration)."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import os

from mint_ladder_bot.config import Config
from mint_ladder_bot.models import LotInfo, RuntimeState
from mint_ladder_bot.state import load_state, save_state_atomic
from mint_ladder_bot.sniper_engine import detect_all, filter_candidate, create_lot_in_state, persist_sniper_lot
from mint_ladder_bot.sniper_engine.launch_detector import LaunchCandidate
from mint_ladder_bot.sniper_engine.token_filter import REASON_OK, REASON_BLOCKLIST
from mint_ladder_bot.lot_invariants import check_duplicate_lot_for_tx, check_lot_invariants


def test_detect_all_test_mints():
    os.environ.pop("SNIPER_TEST_MINTS", None)
    assert len(detect_all(5)) == 0
    os.environ["SNIPER_TEST_MINTS"] = "So11111111111111111111111111111111111111112,TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    try:
        candidates = detect_all(5)
        assert len(candidates) >= 1
        assert all(c.source == "test" for c in candidates)
        assert candidates[0].mint
    finally:
        os.environ.pop("SNIPER_TEST_MINTS", None)


def test_filter_candidate_pass():
    c = LaunchCandidate(
        mint="So11111111111111111111111111111111111111112",
        source="test",
        detected_at=datetime.now(timezone.utc),
        metadata={"symbol": "WSOL", "name": "Wrapped SOL"},
    )
    r = filter_candidate(c, require_metadata=False, min_liquidity_usd=0.0)
    assert r.passed is True
    assert r.reason == REASON_OK
    assert r.score_breakdown is not None


def test_filter_candidate_blocklist():
    c = LaunchCandidate(mint="BadMint1111111111111111111111111111111111111111", source="test", detected_at=datetime.now(timezone.utc))
    r = filter_candidate(c, blocklist_mints={"BadMint1111111111111111111111111111111111111111"})
    assert r.passed is False
    assert r.reason == REASON_BLOCKLIST


def test_create_lot_in_state_and_persist():
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        status_path = Path(tmp) / "status.json"
        status_path.write_text(
            '{"version":1,"created_at":"2026-01-01T00:00:00Z","wallet":"11111111111111111111111111111111",'
            '"rpc":{"endpoint":"https://mainnet.example.com"},"sol":{"lamports":0,"sol":0.0},"mints":[]}'
        )
        state = load_state(state_path, status_path)
        lot = create_lot_in_state(
            state,
            mint="TestMint1111111111111111111111111111111111111111",
            token_amount_raw=1_000_000,
            entry_price_sol_per_token=1e-6,
            tx_signature="txsig123",
            trading_bag_raw="1000000",
            moonbag_raw="0",
            program_or_venue="jupiter",
        )
        assert lot.lot_id
        assert lot.mint == "TestMint1111111111111111111111111111111111111111"
        assert lot.tx_signature == "txsig123"
        assert int(lot.remaining_amount) == 1_000_000
        persist_sniper_lot(state_path, status_path, state, journal_path=None, created_lot=lot)
        assert state_path.exists()


def test_no_duplicate_lot_for_same_tx():
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        status_path = Path(tmp) / "status.json"
        status_path.write_text(
            '{"version":1,"created_at":"2026-01-01T00:00:00Z","wallet":"11111111111111111111",'
            '"rpc":{"endpoint":"https://mainnet.example.com"},"sol":{"lamports":0,"sol":0.0},"mints":[]}'
        )
        state = load_state(state_path, status_path)
        create_lot_in_state(
            state,
            mint="MintA1111111111111111111111111111111111111111",
            token_amount_raw=500_000,
            entry_price_sol_per_token=2e-6,
            tx_signature="same_tx_sig",
            trading_bag_raw="500000",
            program_or_venue="jupiter",
        )
        assert check_duplicate_lot_for_tx(state, "same_tx_sig", "MintA1111111111111111111111111111111111111111", None) is True
        assert check_duplicate_lot_for_tx(state, "other_tx", "MintA1111111111111111111111111111111111111111", None) is False
