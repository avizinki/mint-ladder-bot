from __future__ import annotations

from datetime import datetime, timezone

from mint_ladder_bot.models import LotInfo, RuntimeMintState, RuntimeState, StepExecutionInfo
from mint_ladder_bot.runner import _add_sell_accounting, _debit_lots_fifo


def test_multi_child_swap_mismatch_accounting_uses_observed_delta():
    """
    Multi-child swap with post-swap mismatch must debit lots and sell accounting
    according to the observed wallet delta, not the requested child sizes.

    Scenario (values in raw units):
    - Initial wallet balance = 200
    - Step is fractured into two children: requested 100 + 100
    - Chain actually executes 20 + 120 (post-swap wallet deltas)
    - Observed wallet delta for the step = 140

    This test simulates the accounting layer for that scenario and asserts:
    - sum(child_observed_deltas) equals the observed wallet delta
    - _debit_lots_fifo debits exactly the observed amount
    - executed_steps[].sold_raw equals the observed amount
    - sell accounting invariant holds:
        sold_bot_raw + sold_external_raw == sum(executed_steps[].sold_raw)
    """

    mint = "MINT_SWAP_MISMATCH"
    initial_amount = 200_000
    # Two fractured children, but on-chain only 20% of the first and 120% of the second execute.
    observed_child_deltas = [20_000, 120_000]
    observed_total = sum(observed_child_deltas)

    # Initial state: one tx-derived lot fully representing the wallet balance.
    lot = LotInfo.create(
        mint=mint,
        token_amount_raw=initial_amount,
        entry_price=1e-6,
        confidence="known",
        source="tx_exact",
    )
    lot.remaining_amount = str(initial_amount)

    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw=str(initial_amount),
        moonbag_raw="0",
        lots=[lot],
    )
    state = RuntimeState(
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        mints={mint: ms},
    )

    # Simulate wallet balance before and after the fractured step using observed deltas.
    wallet_balance_before = initial_amount
    wallet_balance_after = initial_amount - observed_total
    assert wallet_balance_after == 60_000
    assert observed_total == wallet_balance_before - wallet_balance_after

    # Assert requirement (1): sum of child deltas equals observed wallet delta for the step.
    assert observed_total == sum(observed_child_deltas)

    # Apply observed per-child debits through the real FIFO lot engine.
    for delta in observed_child_deltas:
        _debit_lots_fifo(ms, delta)

    # Requirement (2): lots must be debited exactly by the observed total.
    assert int(ms.lots[0].remaining_amount) == initial_amount - observed_total
    # In production, trading_bag_raw is recomputed from lots after debits; mirror that here.
    remaining_total = sum(int(getattr(l, "remaining_amount", 0) or 0) for l in ms.lots)
    assert remaining_total == initial_amount - observed_total
    ms.trading_bag_raw = str(remaining_total)

    # Construct a single executed step that represents the fractured execution.
    step_key = "step_multi_child_swap"
    executed_info = StepExecutionInfo(
        sig="fractured_sig",
        time=datetime.now(tz=timezone.utc),
        sold_raw=str(observed_total),
        sol_out=0.123,  # value is irrelevant for this invariant
    )
    ms.executed_steps[step_key] = executed_info

    # Requirement (3) is encoded by construction: executed_steps[].sold_raw equals observed_total.
    assert int(ms.executed_steps[step_key].sold_raw) == observed_total

    # Run sell accounting using the actual implementation.
    _add_sell_accounting(
        ms,
        bot_delta=observed_total,
        journal_path=None,
        mint=mint,
        step_key=step_key,
        tx_sig=executed_info.sig,
        sold_raw=observed_total,
        source="bot",
    )

    # Requirement (4): sell accounting invariant must hold.
    total_sold_steps = sum(
        int(getattr(s, "sold_raw", 0) or 0) for s in ms.executed_steps.values()
    )
    sold_bot = int(ms.sold_bot_raw or 0) if getattr(ms, "sold_bot_raw", None) is not None else 0
    sold_ext = int(ms.sold_external_raw or 0) if getattr(ms, "sold_external_raw", None) is not None else 0

    assert sold_ext == 0
    assert sold_bot == observed_total
    assert sold_bot + sold_ext == total_sold_steps

