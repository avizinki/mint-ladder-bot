"""
Strategy abstraction for mint-ladder-bot.

Defines the Step type and Strategy protocol so the runtime can invoke
"the strategy for this lane" instead of inlining ladder logic. Aligns with
docs/trading/mint-ladder-bot-strategy-interface.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple

from ..config import Config
from ..models import MintStatus, RuntimeMintState


@dataclass
class Step:
    """
    A single sell step: stable key, price target (multiple and/or absolute),
    and raw sell amount. Order within a list is significant (execution order).
    """
    step_key: str
    multiple: Optional[float] = None
    target_price_sol_per_token: float = 0.0
    sell_amount_raw: int = 0


class ContextProtocol(Protocol):
    """Per-mint, per-cycle derived inputs for step construction."""

    volatility_regime: str
    momentum_regime: str
    liquidity_cap_raw: Optional[int]
    spike_mode: bool


class StrategyProtocol(Protocol):
    """
    Strategy interface: build steps for a mint and optionally resolve
    the next unexecuted step. Strategy does not execute swaps.
    """

    def build_steps(
        self,
        mint_status: MintStatus,
        mint_state: RuntimeMintState,
        config: Config,
        context: ContextProtocol,
    ) -> List[Step]:
        """Return ordered list of steps for this mint. Empty if no steps."""
        ...

    def get_next_unexecuted_step(
        self,
        steps: List[Step],
        mint_state: RuntimeMintState,
    ) -> Optional[Tuple[Step, str]]:
        """
        Return (step, step_key) for the first step not in mint_state.executed_steps,
        or None if all steps are executed.
        """
        ...


def get_next_unexecuted_step(
    steps: List[Step],
    mint_state: RuntimeMintState,
) -> Optional[Tuple[Step, str]]:
    """
    Shared helper: first step whose step_key is not in mint_state.executed_steps.
    Runner or strategy can use this to satisfy the interface.
    CEO: Ladder progress uses only bot-executed steps (step_key = str(step_id)).
    External sells (executed_steps keys starting with "ext_") do NOT advance ladder.
    """
    executed = mint_state.executed_steps or {}
    for step in steps:
        if step.step_key not in executed:
            return (step, step.step_key)
    return None
