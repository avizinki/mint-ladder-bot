"""
Integration tests — CEO directive §13: backfill engine.

Scenarios: reconstruct_wallet_lots, verify_lot_integrity.
"""
import pytest
from datetime import datetime, timezone

from mint_ladder_bot.backfill_engine import reconstruct_wallet_lots, verify_lot_integrity
from mint_ladder_bot.data.helius_adapter import WalletTxEvent


def test_reconstruct_wallet_lots_empty():
    lots = reconstruct_wallet_lots("wallet", [])
    assert lots == []


def test_reconstruct_wallet_lots_buy_sell():
    events = [
        WalletTxEvent(
            signature="sig1",
            timestamp=datetime.now(timezone.utc),
            mint="mintA",
            token_delta=1000,
            sol_delta=-5000,
            type="buy",
        ),
        WalletTxEvent(
            signature="sig2",
            timestamp=datetime.now(timezone.utc),
            mint="mintA",
            token_delta=-400,
            sol_delta=2500,
            type="sell",
        ),
    ]
    lots = reconstruct_wallet_lots("wallet", events)
    assert len(lots) >= 1


def test_verify_lot_integrity_ok():
    lots = [{"mint": "mint1", "entry_sig": "s1", "entry_token_raw": 100}]
    result = verify_lot_integrity(lots, {})
    assert result["ok"] is True
    assert result["errors"] == []


def test_verify_lot_integrity_missing_mint():
    lots = [{"entry_sig": "s1"}]
    result = verify_lot_integrity(lots, {})
    assert result["ok"] is False
    assert any("mint" in e for e in result["errors"])
