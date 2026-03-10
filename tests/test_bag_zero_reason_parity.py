from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mint_ladder_bot.bag_zero_reason import classify_bag_zero_reason
from mint_ladder_bot.models import (
    FailureInfo,
    LotInfo,
    MintStatus,
    RuntimeMintState,
    StatusFile,
)


def _status_for_mint(m: MintStatus) -> StatusFile:
    from mint_ladder_bot.models import RpcInfo, SolBalance

    return StatusFile(
        created_at=datetime.now(tz=timezone.utc),
        wallet="test",
        rpc=RpcInfo(endpoint="http://test"),
        sol=SolBalance(lamports=0, sol=0.0),
        mints=[m],
    )


def _mint_status(balance_raw: int = 1000) -> MintStatus:
    return MintStatus(
        mint="M",
        token_account="acc",
        decimals=6,
        balance_ui=balance_raw / 1e6,
        balance_raw=str(balance_raw),
        symbol="M",
    )


def _mint_state_with(lots, **kwargs) -> RuntimeMintState:
    return RuntimeMintState(
        entry_price_sol_per_token=kwargs.get("entry_price", 0.0),
        trading_bag_raw=kwargs.get("trading_bag_raw", "0"),
        moonbag_raw=kwargs.get("moonbag_raw", "0"),
        lots=lots,
        failures=kwargs.get("failures", FailureInfo()),
        protection_state=kwargs.get("protection_state", "active"),
        quarantine_until=kwargs.get("quarantine_until"),
        tradable=kwargs.get("tradable"),
    )


def _truth_bag_zero_reason(ms: RuntimeMintState, balance_raw: int) -> str:
    # Dashboard truth expects dict-shaped status_mint, not a Pydantic model.
    mstatus = _mint_status(balance_raw=balance_raw)
    status = _status_for_mint(mstatus)
    status_mint_dict = status.mints[0].model_dump()
    from mint_ladder_bot.dashboard_truth import token_truth

    mint_data = ms.dict()
    truth = token_truth(mstatus.mint, mint_data, status_mint_dict, decimals=mstatus.decimals, symbol=mstatus.symbol)
    return truth.get("bag_zero_reason")


def test_bag_zero_reason_paused_precedence():
    """Paused/quarantine must win over unknown_entry_lots when both apply."""
    lot = LotInfo.create(
        mint="M",
        token_amount_raw=1000,
        entry_price=None,
        confidence="unknown",
        source="tx_parsed",
    )
    lot.remaining_amount = "1000"
    failures = FailureInfo(
        count=1,
        last_error="reconciliation_mismatch",
        paused_until=datetime.now(tz=timezone.utc) + timedelta(minutes=5),
    )
    ms = _mint_state_with(
        [lot],
        trading_bag_raw="0",
        failures=failures,
    )
    mint_dict = ms.dict()
    reason = classify_bag_zero_reason(mint_dict, wallet_balance_raw=1000)
    assert reason == "paused_or_quarantine"
    assert _truth_bag_zero_reason(ms, balance_raw=1000) == "paused_or_quarantine"


def test_bag_zero_reason_excluded_from_ladder():
    """When tradable=False and tx-derived inventory exists, reason is excluded_from_ladder."""
    lot = LotInfo.create(
        mint="M",
        token_amount_raw=1000,
        entry_price=None,
        confidence="unknown",
        source="tx_parsed",
    )
    lot.remaining_amount = "1000"
    ms = _mint_state_with(
        [lot],
        trading_bag_raw="0",
        tradable=False,
    )
    mint_dict = ms.dict()
    reason = classify_bag_zero_reason(mint_dict, wallet_balance_raw=1000)
    assert reason == "excluded_from_ladder"
    assert _truth_bag_zero_reason(ms, balance_raw=1000) == "excluded_from_ladder"


def test_bag_zero_reason_bootstrap_only():
    """Only bootstrap lots => non_tradable_sources_only."""
    lot = LotInfo.create(
        mint="M",
        token_amount_raw=1000,
        entry_price=None,
        confidence="unknown",
        source="bootstrap_snapshot",
    )
    lot.remaining_amount = "1000"
    ms = _mint_state_with(
        [lot],
        trading_bag_raw="0",
    )
    mint_dict = ms.dict()
    reason = classify_bag_zero_reason(mint_dict, wallet_balance_raw=1000)
    assert reason == "non_tradable_sources_only"
    assert _truth_bag_zero_reason(ms, balance_raw=1000) == "non_tradable_sources_only"


def test_bag_zero_reason_transfer_like_only():
    """Only transfer-like lots (neither tx-derived nor bootstrap) => non_tradable_sources_only."""
    lot = LotInfo.create(
        mint="M",
        token_amount_raw=1000,
        entry_price=None,
        confidence="unknown",
        source="transfer_received_unknown",
    )
    lot.remaining_amount = "1000"
    ms = _mint_state_with(
        [lot],
        trading_bag_raw="0",
    )
    mint_dict = ms.dict()
    reason = classify_bag_zero_reason(mint_dict, wallet_balance_raw=1000)
    assert reason == "non_tradable_sources_only"
    assert _truth_bag_zero_reason(ms, balance_raw=1000) == "non_tradable_sources_only"


def test_bag_zero_reason_zero_balance_control():
    """Zero balance / non-bag-zero control => other."""
    ms = _mint_state_with(
        [],
        trading_bag_raw="0",
    )
    mint_dict = ms.dict()
    reason = classify_bag_zero_reason(mint_dict, wallet_balance_raw=0)
    assert reason == "other"
    # token_truth will not set bag_zero_reason when balance_raw == 0
    assert _truth_bag_zero_reason(ms, balance_raw=0) is None

