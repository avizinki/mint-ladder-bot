"""
Sniper strategy: safe early-entry token strategy with short (quick-exit) ladder.

Design and integration contract: docs/SNIPER_LADDER_DESIGN.md.
Implements StrategyProtocol from strategies/base.py. No network calls; step generation only.
Sniper + ladder operate as one system: sniper buys → lot created → ladder arms → partial TPs → runner remains.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Set, Tuple

from ..config import Config
from ..models import MintStatus, RuntimeMintState

from .base import ContextProtocol, Step, StrategyProtocol, get_next_unexecuted_step


def _sniper_config_float(config: Config, attr: str, env_name: str, default: float) -> float:
    val = getattr(config, attr, None)
    if val is not None:
        return float(val)
    env = os.getenv(env_name)
    if env is not None:
        try:
            return float(env)
        except ValueError:
            pass
    return default


def _sniper_config_int(config: Config, attr: str, env_name: str, default: int) -> int:
    val = getattr(config, attr, None)
    if val is not None:
        return int(val)
    env = os.getenv(env_name)
    if env is not None:
        try:
            return int(env)
        except ValueError:
            pass
    return default


def _sniper_config_path(config: Config, attr: str, env_name: str) -> Optional[Path]:
    val = getattr(config, attr, None)
    if val is not None:
        return Path(val) if val else None
    env = os.getenv(env_name)
    if env is None or not env.strip():
        return None
    return Path(env.strip())


def _load_blacklist(path: Optional[Path]) -> Set[str]:
    if path is None or not path.exists():
        return set()
    mints: Set[str] = set()
    try:
        with open(path, "r") as f:
            for line in f:
                mint = line.strip()
                if mint and not mint.startswith("#"):
                    mints.add(mint)
    except OSError:
        pass
    return mints


# Default target multiples for quick-exit ladder (min 1.5x, max 4.0x).
def _default_quick_exit_multiples(step_count: int) -> List[float]:
    if step_count <= 0:
        return []
    if step_count == 1:
        return [2.0]
    min_mult, max_mult = 1.5, 4.0
    return [
        min_mult + (max_mult - min_mult) * i / (step_count - 1)
        for i in range(step_count)
    ]


class SniperStrategy:
    """
    Sniper strategy: short ladder (quick exit), liquidity threshold, max entry size,
    blacklist. Rug-risk: blacklist check only (no network, no heuristic).
    """

    def build_steps(
        self,
        mint_status: MintStatus,
        mint_state: RuntimeMintState,
        config: Config,
        context: ContextProtocol,
    ) -> List[Step]:
        """
        Build a short quick-exit ladder. Returns [] if mint is blacklisted,
        liquidity below threshold, position over max entry, or no trading bag.
        """
        mint = mint_status.mint
        min_liquidity_usd = _sniper_config_float(
            config, "sniper_min_liquidity_usd", "SNIPER_MIN_LIQUIDITY_USD", 5000.0
        )
        max_entry_sol = _sniper_config_float(
            config, "sniper_max_entry_sol", "SNIPER_MAX_ENTRY_SOL", 1.0
        )
        quick_exit_steps = _sniper_config_int(
            config, "sniper_quick_exit_steps", "SNIPER_QUICK_EXIT_STEPS", 5
        )
        blacklist_path = _sniper_config_path(
            config, "sniper_blacklist_path", "SNIPER_BLACKLIST_PATH"
        )

        blacklist = _load_blacklist(blacklist_path)
        if mint in blacklist:
            return []

        liquidity_usd = None
        if mint_status.market and mint_status.market.dexscreener:
            liquidity_usd = mint_status.market.dexscreener.liquidity_usd
        if liquidity_usd is None or liquidity_usd < min_liquidity_usd:
            return []

        try:
            trading_bag_raw = int(mint_state.trading_bag_raw)
        except (ValueError, TypeError):
            return []
        if trading_bag_raw <= 0:
            return []

        entry_price = mint_state.working_entry_price_sol_per_token or mint_state.entry_price_sol_per_token
        if entry_price is None or entry_price <= 0:
            return []
        entry_price = float(entry_price)
        decimals = mint_status.decimals
        if decimals is None or decimals < 0:
            decimals = 9
        token_amount = trading_bag_raw / (10 ** decimals)
        entry_value_sol = token_amount * entry_price
        if entry_value_sol > max_entry_sol:
            return []

        if quick_exit_steps <= 0:
            return []

        multiples = _default_quick_exit_multiples(quick_exit_steps)
        step_count = len(multiples)
        if step_count == 0:
            return []

        amount_per_step = trading_bag_raw // step_count
        if amount_per_step <= 0:
            return []

        steps: List[Step] = []
        for i, mult in enumerate(multiples):
            step_key = f"sniper_{i + 1}"
            target_price = entry_price * mult
            sell_raw = amount_per_step
            if i == step_count - 1:
                sell_raw = trading_bag_raw - (amount_per_step * (step_count - 1))
            steps.append(
                Step(
                    step_key=step_key,
                    multiple=mult,
                    target_price_sol_per_token=target_price,
                    sell_amount_raw=sell_raw,
                )
            )
        return steps

    def get_next_unexecuted_step(
        self,
        steps: List[Step],
        mint_state: RuntimeMintState,
    ) -> Optional[Tuple[Step, str]]:
        return get_next_unexecuted_step(steps, mint_state)
