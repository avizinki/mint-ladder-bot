"""Tests for lot_invariants: sold <= bought, remaining >= 0, no duplicate lot per tx."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from pathlib import Path

from mint_ladder_bot.models import LotInfo, RuntimeMintState, RuntimeState, StepExecutionInfo, FailureInfo, BuybackInfo, BootstrapInfo
from mint_ladder_bot.lot_invariants import (
    check_lot_invariants,
    check_duplicate_lot_for_tx,
    check_all_state_invariants,
    _lot_bought_raw,
    _lot_sold_raw,
    _lot_remaining_raw,
)


def test_lot_sold_and_remaining():
    lot = LotInfo(mint="m1", token_amount="1000", remaining_amount="600")
    assert _lot_bought_raw(lot) == 1000
    assert _lot_remaining_raw(lot) == 600
    assert _lot_sold_raw(lot) == 400


def test_lot_invariants_pass():
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw="1000",
        moonbag_raw="0",
        lots=[
            LotInfo(mint="m1", token_amount="1000", remaining_amount="600", tx_signature="sig1"),
        ],
        executed_steps={"step1": StepExecutionInfo(sig="s1", time=datetime.now(timezone.utc), sold_raw="400", sol_out=0.01)},
    )
    errors = check_lot_invariants("m1", ms, event_journal_path=None)
    assert errors == []


def test_lot_invariants_sold_gt_bought():
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw="1000",
        moonbag_raw="0",
        lots=[LotInfo(mint="m1", token_amount="1000", remaining_amount="0")],  # sold 1000, but we'll corrupt to 1200
    )
    ms.lots[0].remaining_amount = "-200"  # sold = 1000 - (-200) = 1200 > 1000
    errors = check_lot_invariants("m1", ms, event_journal_path=None)
    assert any("sold_raw" in e and "bought_raw" in e for e in errors)


def test_lot_invariants_remaining_negative():
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw="1000",
        moonbag_raw="0",
        lots=[LotInfo(mint="m1", token_amount="1000", remaining_amount="-100")],
    )
    errors = check_lot_invariants("m1", ms, event_journal_path=None)
    assert any("remaining_raw" in e and "< 0" in e for e in errors)


def test_check_duplicate_lot_for_tx():
    state = RuntimeState(
        version=1,
        started_at=datetime.now(timezone.utc),
        status_file="status.json",
        mints={
            "m1": RuntimeMintState(
                entry_price_sol_per_token=1e-6,
                trading_bag_raw="1000",
                moonbag_raw="0",
                lots=[LotInfo(mint="m1", token_amount="1000", remaining_amount="1000", tx_signature="abc123")],
            ),
        },
    )
    assert check_duplicate_lot_for_tx(state, "abc123", "m1", None) is True
    assert check_duplicate_lot_for_tx(state, "other_sig", "m1", None) is False
    assert check_duplicate_lot_for_tx(state, "abc123", "m2", None) is False


def test_check_all_state_invariants():
    state = RuntimeState(
        version=1,
        started_at=datetime.now(timezone.utc),
        status_file="status.json",
        mints={
            "m1": RuntimeMintState(
                entry_price_sol_per_token=1e-6,
                trading_bag_raw="1000",
                moonbag_raw="0",
                lots=[LotInfo(mint="m1", token_amount="1000", remaining_amount="1000")],
            ),
        },
    )
    errors = check_all_state_invariants(state, event_journal_path=None)
    assert errors == []
