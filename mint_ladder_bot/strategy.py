from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from rich.console import Console
from rich.table import Table

from .config import Config
from .models import (
    LiquidityCapInfo,
    MintStatus,
    RuntimeMintState,
    RuntimeState,
    StatusFile,
    VolatilityInfo,
)


logger = logging.getLogger(__name__)

LADDER_MULTIPLES: List[float] = [
    1.10,
    1.20,
    1.30,
    1.40,
    1.50,
    1.65,
    1.80,
    2.00,
    2.25,
    2.50,
    3.00,
    3.50,
    4.00,
    5.00,
    6.00,
    7.50,
    10.0,
    15.0,
    20.0,
    30.0,
]

# Interpreted as percent values (e.g. 0.25 = 0.25% of trading bag).
LADDER_PERCENTS: List[float] = [
    0.25,
    0.25,
    0.35,
    0.35,
    0.50,
    0.50,
    0.60,
    0.75,
    1.00,
    1.00,
    1.25,
    1.50,
    1.70,
    2.00,
    2.50,
    3.00,
    3.50,
    4.00,
    4.50,
    5.00,
]


@dataclass
class LadderStep:
    step_id: int
    multiple: float
    target_price_sol_per_token: float
    sell_amount_raw: int


@dataclass
class DynamicContext:
    """Snapshot of dynamic ladder inputs and derived regimes for a mint."""

    volatility_regime: str
    momentum_regime: str
    liquidity_cap_raw: Optional[int]
    spike_mode: bool = False  # volume-spike: pump + strong momentum + liquidity; tighter early spacing


def compute_trading_bag(balance_raw: str, trading_bag_pct: float) -> tuple[int, int]:
    """
    Return (trading_bag_raw, moonbag_raw) from a raw integer balance string.
    """

    total_raw = int(balance_raw)
    if total_raw <= 0 or trading_bag_pct <= 0:
        return 0, total_raw
    trading_bag_raw = int(total_raw * trading_bag_pct)
    if trading_bag_raw < 0:
        trading_bag_raw = 0
    if trading_bag_raw > total_raw:
        trading_bag_raw = total_raw
    moonbag_raw = total_raw - trading_bag_raw
    return trading_bag_raw, moonbag_raw


def _base_multiples_for_volatility(regime: str) -> List[float]:
    """
    Choose ladder multiples based on volatility regime.

    High vol  -> tighter early steps.
    Low  vol  -> wider spacing.
    Medium    -> fallback to static defaults.
    """

    if regime == "high":
        return [
            1.05,
            1.10,
            1.18,
            1.26,
            1.35,
            1.45,
            1.55,
            1.70,
            1.90,
            2.10,
            2.40,
            2.80,
            3.20,
            4.00,
            5.00,
            6.50,
            8.50,
            11.0,
            15.0,
            25.0,
        ]
    if regime == "low":
        return [
            1.12,
            1.25,
            1.40,
            1.55,
            1.70,
            1.90,
            2.10,
            2.40,
            2.80,
            3.20,
            3.80,
            4.50,
            5.50,
            7.00,
            9.00,
            12.0,
            16.0,
            20.0,
            26.0,
            32.0,
        ]
    # medium / unknown -> static default
    return LADDER_MULTIPLES


def _scaled_percents_for_momentum(regime: str) -> List[float]:
    """
    Scale base ladder percents based on momentum regime.

    Strong    -> slightly larger sells overall.
    Weak      -> smaller sells, effectively lengthening cooldown per amount.
    Neutral   -> base profile.
    """

    if regime == "strong":
        factor = 1.3
    elif regime == "weak":
        factor = 0.7
    else:
        factor = 1.0

    # LADDER_PERCENTS are already expressed as "percent of trading bag".
    # Momentum scaling should only nudge them up/down, not renormalize.
    return [p * factor for p in LADDER_PERCENTS]


def build_dynamic_ladder_for_mint(
    mint_status: MintStatus,
    runtime_state: RuntimeMintState,
    context: DynamicContext,
) -> List[LadderStep]:
    """
    Build a dynamic ladder for a mint, adapting spacing and size per step.

    - Volatility regime chooses the base multiples (spacing).
    - Momentum regime scales sell percents (aggressiveness).
    - Liquidity cap enforces a hard upper bound per step.
    - Executed steps are tracked by caller via their stable step_id.
    """

    entry_price = getattr(
        runtime_state, "working_entry_price_sol_per_token", None
    ) or runtime_state.entry_price_sol_per_token
    if entry_price <= 0:
        raise ValueError("entry_price_sol_per_token must be > 0 for ladder construction")

    trading_bag_raw = int(runtime_state.trading_bag_raw)
    if trading_bag_raw <= 0:
        return []

    # Volume-spike mode: tighter early spacing + slight micro-capture on first 3 steps only.
    if context.spike_mode:
        multiples = _base_multiples_for_volatility("high")
        percents = list(_scaled_percents_for_momentum(context.momentum_regime))
        for i in range(min(3, len(percents))):
            percents[i] *= 1.1
        percents[0] = min(percents[0], 5.0)  # keep first-step cap at 5% of bag
    else:
        multiples = _base_multiples_for_volatility(context.volatility_regime)
        percents = _scaled_percents_for_momentum(context.momentum_regime)
    steps: List[LadderStep] = []
    remaining = trading_bag_raw
    # If we do not have a liquidity cap, fall back to a small fraction of the bag
    # rather than the full trading bag.
    fallback_cap = max(int(trading_bag_raw * 0.002), 1)  # ~0.2% of bag
    liquidity_cap_raw = context.liquidity_cap_raw if context.liquidity_cap_raw is not None else fallback_cap

    for idx, (multiple, pct) in enumerate(zip(multiples, percents), start=1):
        if remaining <= 0:
            sell_raw = 0
        else:
            base_step_raw = int(trading_bag_raw * (pct / 100.0))
            # Respect remaining trading bag and the liquidity/impact cap.
            candidate = min(base_step_raw, remaining, liquidity_cap_raw)
            sell_raw = max(candidate, 0)
            # First step never exceeds 5% of trading bag (protects against abnormal momentum scaling).
            if idx == 1:
                sell_raw = min(sell_raw, max(int(trading_bag_raw * 0.05), 0))
            remaining -= sell_raw

        steps.append(
            LadderStep(
                step_id=idx,
                multiple=multiple,
                target_price_sol_per_token=entry_price * multiple,
                sell_amount_raw=sell_raw,
            )
        )

    return steps


def build_ladder_for_mint(
    mint_status: MintStatus,
    runtime_state: RuntimeMintState,
    config: Optional[Config] = None,
) -> List[LadderStep]:
    """
    Backward-compatible wrapper for static ladder construction.

    Older tests and tooling import build_ladder_for_mint(mint_status, runtime_state);
    internally we now use build_dynamic_ladder_for_mint with a conservative default
    context (medium volatility, neutral momentum, no explicit liquidity cap).
    """
    # Default regimes for tests / legacy callers; planning and runtime use the
    # richer context in print_plan_for_status and runner.run_bot.
    ctx = DynamicContext(
        volatility_regime="medium",
        momentum_regime="neutral",
        liquidity_cap_raw=None,
    )
    return build_dynamic_ladder_for_mint(mint_status, runtime_state, ctx)


def _eligible_mints_for_plan(status_file: StatusFile) -> Sequence[MintStatus]:
    eligible: List[MintStatus] = []
    for m in status_file.mints:
        if m.balance_ui <= 0:
            continue
        if m.entry.entry_price_sol_per_token <= 0:
            continue
        # Exclude SOL/WSOL if present as SPL tokens.
        if m.mint in {
            "So11111111111111111111111111111111111111112",  # WSOL
        }:
            continue
        eligible.append(m)
    return eligible


def print_plan_for_status(
    status_file: StatusFile,
    config: Config,
    state: Optional[RuntimeState] = None,
) -> None:
    """
    Print per-mint ladder steps using the current configuration.
    If state is provided, use its trading_bag per mint and show a Done column for executed steps.
    """

    console = Console()
    eligible = _eligible_mints_for_plan(status_file)

    if not eligible:
        console.print("No eligible mints with non-zero balance and entry price > 0.")
        return

    for mint_status in eligible:
        entry_price = mint_status.entry.entry_price_sol_per_token
        if entry_price <= 0:
            continue

        mint_state = state.mints.get(mint_status.mint) if state else None
        if mint_state is not None:
            trading_bag_raw = int(mint_state.trading_bag_raw)
            moonbag_raw = int(mint_state.moonbag_raw)
        else:
            trading_bag_raw, moonbag_raw = compute_trading_bag(
                balance_raw=mint_status.balance_raw,
                trading_bag_pct=config.trading_bag_pct,
            )

        # For planning we do not have live volatility/momentum; derive a conservative
        # context from static defaults and on-chain liquidity.
        liq_info = LiquidityCapInfo()
        ds = mint_status.market.dexscreener
        if ds.liquidity_usd is not None and ds.price_usd:
            try:
                # Match the runtime liquidity behaviour: 0.2%–0.5% of LP depending
                # on strength, so plan output reflects what the runner will do.
                liq = ds.liquidity_usd
                if liq >= 5_000_000:
                    frac = 0.005
                elif liq >= 1_000_000:
                    frac = 0.003
                else:
                    frac = 0.002
                safe_value_usd = liq * frac
                max_tokens = safe_value_usd / ds.price_usd
                liq_info.max_sell_raw = int(max_tokens * (10 ** mint_status.decimals))
            except Exception:
                liq_info.max_sell_raw = None

        # Default regimes for static planning output.
        vol_regime = "medium"
        if ds.volume24h_usd and ds.volume24h_usd > 1_000_000:
            vol_regime = "high"
        elif ds.volume24h_usd is not None and ds.volume24h_usd < 50_000:
            vol_regime = "low"

        txs = ds.txns24h
        buys = txs.buys or 0
        sells = txs.sells or 0
        momentum_regime = "neutral"
        if buys > sells * 1.5:
            momentum_regime = "strong"
        elif sells > buys * 1.5:
            momentum_regime = "weak"

        runtime = RuntimeMintState(
            entry_price_sol_per_token=entry_price,
            trading_bag_raw=str(trading_bag_raw),
            moonbag_raw=str(moonbag_raw),
            liquidity_cap=liq_info,
        )
        ctx = DynamicContext(
            volatility_regime=vol_regime,
            momentum_regime=momentum_regime,
            liquidity_cap_raw=runtime.liquidity_cap.max_sell_raw,
        )
        steps = build_dynamic_ladder_for_mint(mint_status, runtime, ctx)
        executed = (mint_state.executed_steps or {}) if mint_state else {}

        short_mint = ("…" + mint_status.mint[-8:]) if len(mint_status.mint) > 8 else (mint_status.mint or "?")
        pair_name = (mint_status.symbol or mint_status.name or "").strip() or short_mint
        balance_ui = (trading_bag_raw + moonbag_raw) / (10 ** mint_status.decimals) if mint_state is not None else mint_status.balance_ui
        console.rule(
            f"{pair_name}  balance={balance_ui:.6f} entry={entry_price:.9f} SOL  "
            f"[vol={vol_regime} mom={momentum_regime} liq_cap={runtime.liquidity_cap.max_sell_raw or 0}]"
        )

        table = Table(show_header=True, header_style="bold")
        table.add_column("Step")
        table.add_column("Multiple")
        table.add_column("Target Price (SOL)")
        table.add_column("Sell Amount (tokens)")
        table.add_column("Approx SOL at Target")
        table.add_column("Done")

        trading_bag_tokens = trading_bag_raw / (10 ** mint_status.decimals)

        for idx, step in enumerate(steps, start=1):
            sell_tokens = step.sell_amount_raw / (10 ** mint_status.decimals)
            approx_sol = sell_tokens * step.target_price_sol_per_token
            # Executed steps keyed by step_id; legacy multiple keys accepted for backward compatibility only.
            step_key = str(step.step_id)
            legacy_key = f"{step.multiple:.2f}"
            done = "✓" if (step_key in executed or legacy_key in executed) else ""
            table.add_row(
                str(step.step_id),
                f"{step.multiple:.2f}",
                f"{step.target_price_sol_per_token:.9f}",
                f"{sell_tokens:.6f}",
                f"{approx_sol:.6f}",
                done,
            )

        console.print(table)

