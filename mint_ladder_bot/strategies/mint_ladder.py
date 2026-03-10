"""
First strategy implementation: dynamic 20-step ladder-sell.

Wraps the existing strategy.build_dynamic_ladder_for_mint and LadderStep;
exposes the same step_key convention (str(step_id)) for compatibility with
executed_steps. Runner continues to use strategy.py directly; this module
is for future integration via the strategies interface.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from ..config import Config
from ..models import MintStatus, RuntimeMintState
from ..strategy import (
    DynamicContext,
    build_dynamic_ladder_for_mint,
    LadderStep,
)

from .base import Step, StrategyProtocol, get_next_unexecuted_step


def _ladder_step_to_step(ladder_step: LadderStep) -> Step:
    """Convert LadderStep to interface Step; step_key = str(step_id) for executed_steps compatibility."""
    return Step(
        step_key=str(ladder_step.step_id),
        multiple=ladder_step.multiple,
        target_price_sol_per_token=ladder_step.target_price_sol_per_token,
        sell_amount_raw=ladder_step.sell_amount_raw,
    )


class MintLadderStrategy:
    """
    Dynamic ladder-sell strategy: 20 steps, volatility/momentum/liquidity
    adapted. Implements StrategyProtocol; delegates to build_dynamic_ladder_for_mint.
    """

    def build_steps(
        self,
        mint_status: MintStatus,
        mint_state: RuntimeMintState,
        config: Config,
        context: DynamicContext,
    ) -> List[Step]:
        ladder_steps = build_dynamic_ladder_for_mint(mint_status, mint_state, context)
        return [_ladder_step_to_step(ls) for ls in ladder_steps]

    def get_next_unexecuted_step(
        self,
        steps: List[Step],
        mint_state: RuntimeMintState,
    ) -> Optional[Tuple[Step, str]]:
        return get_next_unexecuted_step(steps, mint_state)
