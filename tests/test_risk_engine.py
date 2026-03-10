"""
Runtime proof: risk engine blocks execution when limits are violated.
CEO directive §4 + integration audit Phase 4.
"""
import pytest
from mint_ladder_bot.risk_engine import (
    RiskLimits,
    block_execution_reason,
    check_liquidity,
    check_trade_size_sol,
    check_sol_reserve,
    check_hourly_sell_cap,
)


def test_risk_engine_blocks_when_liquidity_low():
    limits = RiskLimits(min_liquidity_usd=10_000.0)
    reason = block_execution_reason(
        liquidity_usd=5_000.0,
        slippage_bps=100,
        trade_sol=0.1,
        wallet_sol=2.0,
        sold_this_hour_sol=0.0,
        trading_bag_sol_value=1.0,
        limits=limits,
    )
    assert reason is not None
    assert "liquidity" in reason.lower()


def test_risk_engine_blocks_when_trade_too_large():
    limits = RiskLimits(max_sell_sol_per_trade=0.2)
    reason = block_execution_reason(
        liquidity_usd=50_000.0,
        slippage_bps=100,
        trade_sol=1.0,
        wallet_sol=5.0,
        sold_this_hour_sol=0.0,
        trading_bag_sol_value=2.0,
        limits=limits,
    )
    assert reason is not None
    assert "trade" in reason.lower() or "max" in reason.lower()


def test_risk_engine_blocks_when_wallet_below_reserve():
    limits = RiskLimits(buyback_sol_reserve=0.5)
    reason = block_execution_reason(
        liquidity_usd=50_000.0,
        slippage_bps=100,
        trade_sol=0.1,
        wallet_sol=0.3,
        sold_this_hour_sol=0.0,
        trading_bag_sol_value=1.0,
        limits=limits,
    )
    assert reason is not None
    assert "reserve" in reason.lower() or "sol" in reason.lower()


def test_risk_engine_allows_when_limits_ok():
    limits = RiskLimits(min_liquidity_usd=10_000.0, max_sell_sol_per_trade=1.0)
    reason = block_execution_reason(
        liquidity_usd=50_000.0,
        slippage_bps=100,
        trade_sol=0.1,
        wallet_sol=2.0,
        sold_this_hour_sol=0.0,
        trading_bag_sol_value=1.0,
        limits=limits,
    )
    assert reason is None


def test_check_liquidity_blocks_unknown():
    allowed, reason = check_liquidity(None, RiskLimits())
    assert allowed is False
    assert "unknown" in reason or "liquidity" in reason
