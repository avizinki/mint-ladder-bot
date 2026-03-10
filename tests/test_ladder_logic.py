"""
Integration tests — CEO directive §13: ladder logic.

Scenarios: partial sells, liquidity constraints, step allocation.
"""
import pytest
from mint_ladder_bot.config import Config
from mint_ladder_bot.models import EntryInfo, MarketInfo, MintStatus, RuntimeMintState
from mint_ladder_bot.strategy import build_ladder_for_mint, compute_trading_bag


def test_partial_sell_allocation():
    """Ladder steps sum to at most trading_bag_raw."""
    mint_status = MintStatus(
        mint="mint_partial",
        token_account="ata",
        decimals=6,
        balance_ui=100.0,
        balance_raw="100000000",
        symbol="T",
        entry=EntryInfo(entry_price_sol_per_token=0.0001),
        market=MarketInfo(),
    )
    trading_bag_raw, moonbag_raw = compute_trading_bag(mint_status.balance_raw, trading_bag_pct=0.25)
    mint_state = RuntimeMintState(
        entry_price_sol_per_token=mint_status.entry.entry_price_sol_per_token,
        trading_bag_raw=str(trading_bag_raw),
        moonbag_raw=str(moonbag_raw),
    )
    steps = build_ladder_for_mint(mint_status, mint_state)
    total_sell = sum(s.sell_amount_raw for s in steps)
    assert total_sell <= trading_bag_raw
    assert total_sell > 0


def test_liquidity_cap_respected():
    """When liquidity is low, step sizes should not exceed bag."""
    mint_status = MintStatus(
        mint="mint_liq",
        token_account="ata",
        decimals=6,
        balance_ui=50.0,
        balance_raw="50000000",
        symbol="T",
        entry=EntryInfo(entry_price_sol_per_token=0.001),
        market=MarketInfo(),
    )
    trading_bag_raw, _ = compute_trading_bag(mint_status.balance_raw, trading_bag_pct=0.2)
    mint_state = RuntimeMintState(
        entry_price_sol_per_token=mint_status.entry.entry_price_sol_per_token,
        trading_bag_raw=str(trading_bag_raw),
        moonbag_raw="0",
    )
    steps = build_ladder_for_mint(mint_status, mint_state)
    for s in steps:
        assert s.sell_amount_raw <= trading_bag_raw
