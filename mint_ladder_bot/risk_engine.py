"""
Risk engine — CEO directive §4: mandatory protections before execution.

- Minimum liquidity threshold
- Maximum slippage
- Max trade size per mint
- Hourly sell cap
- Wallet SOL reserve

Blocks execution when limits violated. Never bypass.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


@dataclass
class RiskLimits:
    min_liquidity_usd: float = _env_float("MIN_LIQUIDITY_USD", 10_000.0)
    max_slippage: float = _env_float("MAX_SLIPPAGE", 0.05)
    max_sell_sol_per_trade: float = _env_float("MAX_SELL_SOL_PER_TRADE", 0.2)
    buyback_sol_reserve: float = _env_float("BUYBACK_SOL_RESERVE", 0.5)
    max_sell_bag_fraction_per_hour: float = _env_float("MAX_SELL_BAG_FRACTION_PER_HOUR", 1.0)


def check_liquidity(liquidity_usd: Optional[float], limits: RiskLimits) -> tuple[bool, str]:
    """Return (allowed, reason). Block if liquidity below threshold."""
    if liquidity_usd is None:
        return False, "liquidity_unknown"
    if liquidity_usd < limits.min_liquidity_usd:
        return False, f"liquidity_below_min_{limits.min_liquidity_usd}"
    return True, "ok"


def check_slippage(slippage_bps: int, limits: RiskLimits) -> tuple[bool, str]:
    """slippage_bps in basis points. Block if above max_slippage (e.g. 0.05 = 500 bps)."""
    max_bps = int(limits.max_slippage * 10_000)
    if slippage_bps > max_bps:
        return False, f"slippage_exceeds_max_{max_bps}bps"
    return True, "ok"


def check_trade_size_sol(sol_amount: float, limits: RiskLimits) -> tuple[bool, str]:
    """Block if single trade exceeds max_sell_sol_per_trade."""
    if sol_amount > limits.max_sell_sol_per_trade:
        return False, f"trade_size_exceeds_max_{limits.max_sell_sol_per_trade}_sol"
    return True, "ok"


def check_sol_reserve(wallet_sol: float, reserve: float) -> tuple[bool, str]:
    """Block if wallet SOL would fall below reserve (e.g. for buyback)."""
    if wallet_sol < reserve:
        return False, f"sol_below_reserve_{reserve}"
    return True, "ok"


def check_hourly_sell_cap(
    sold_this_hour_sol: float,
    cap_fraction: float,
    trading_bag_sol_value: float,
) -> tuple[bool, str]:
    """Block if sold this hour exceeds cap fraction of trading bag."""
    cap_sol = cap_fraction * trading_bag_sol_value
    if sold_this_hour_sol >= cap_sol:
        return False, "hourly_sell_cap_reached"
    return True, "ok"


def block_execution_reason(
    liquidity_usd: Optional[float],
    slippage_bps: int,
    trade_sol: float,
    wallet_sol: float,
    sold_this_hour_sol: float,
    trading_bag_sol_value: float,
    limits: Optional[RiskLimits] = None,
) -> Optional[str]:
    """
    Run all risk checks. Returns None if execution allowed, else reason string to log and block.
    """
    lim = limits or RiskLimits()
    ok, reason = check_liquidity(liquidity_usd, lim)
    if not ok:
        return reason
    ok, reason = check_slippage(slippage_bps, lim)
    if not ok:
        return reason
    ok, reason = check_trade_size_sol(trade_sol, lim)
    if not ok:
        return reason
    ok, reason = check_sol_reserve(wallet_sol, lim.buyback_sol_reserve)
    if not ok:
        return reason
    ok, reason = check_hourly_sell_cap(sold_this_hour_sol, lim.max_sell_bag_fraction_per_hour, trading_bag_sol_value)
    if not ok:
        return reason
    return None
