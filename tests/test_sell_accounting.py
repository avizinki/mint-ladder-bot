"""
CEO: Bot vs external sell accounting. Invariant sold_raw == sold_bot_raw + sold_external_raw.
"""
from __future__ import annotations

from datetime import datetime, timezone

from mint_ladder_bot.models import RuntimeMintState, StepExecutionInfo


def _ms(executed_steps=None, sold_bot_raw=None, sold_external_raw=None):
    return RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw="0",
        moonbag_raw="0",
        lots=[],
        executed_steps=executed_steps or {},
        sold_bot_raw=sold_bot_raw,
        sold_external_raw=sold_external_raw,
    )


def test_sell_invariant_from_steps():
    """sold_bot_raw + sold_external_raw must equal sum(executed_steps[].sold_raw)."""
    from mint_ladder_bot.runner import _get_sold_bot_and_external_from_steps

    steps = {
        "1": StepExecutionInfo(sig="s1", time=datetime.now(tz=timezone.utc), sold_raw="100", sol_out=0.01),
        "ext_abc": StepExecutionInfo(sig="s2", time=datetime.now(tz=timezone.utc), sold_raw="200", sol_out=0.02),
    }
    ms = _ms(executed_steps=steps)
    bot, ext = _get_sold_bot_and_external_from_steps(ms)
    assert bot == 100
    assert ext == 200
    assert bot + ext == 300


def test_ensure_sell_accounting_backfill():
    """Backfill populates sold_bot_raw and sold_external_raw from executed_steps when missing."""
    from mint_ladder_bot.runner import _ensure_sell_accounting_backfill

    steps = {
        "1": StepExecutionInfo(sig="s1", time=datetime.now(tz=timezone.utc), sold_raw="50", sol_out=0.005),
        "ext_xyz": StepExecutionInfo(sig="s2", time=datetime.now(tz=timezone.utc), sold_raw="150", sol_out=0.015),
    }
    ms = _ms(executed_steps=steps)
    assert getattr(ms, "sold_bot_raw", None) is None or ms.sold_bot_raw is None
    _ensure_sell_accounting_backfill(ms)
    assert ms.sold_bot_raw == "50"
    assert ms.sold_external_raw == "150"
    assert int(ms.sold_bot_raw) + int(ms.sold_external_raw) == 200


def test_add_sell_accounting_bot():
    """Bot sell increments sold_bot_raw; invariant holds."""
    from mint_ladder_bot.runner import _add_sell_accounting, _ensure_sell_accounting_backfill

    steps = {
        "1": StepExecutionInfo(sig="s1", time=datetime.now(tz=timezone.utc), sold_raw="100", sol_out=0.01),
    }
    ms = _ms(executed_steps=steps)
    _ensure_sell_accounting_backfill(ms)
    assert ms.sold_bot_raw == "100"
    assert ms.sold_external_raw == "0"
    # Simulate another bot sell (runner would have already added to executed_steps; here we only test accounting)
    ms.executed_steps["2"] = StepExecutionInfo(
        sig="s2", time=datetime.now(tz=timezone.utc), sold_raw="50", sol_out=0.005
    )
    _add_sell_accounting(ms, bot_delta=50)
    assert ms.sold_bot_raw == "150"
    assert ms.sold_external_raw == "0"
    assert int(ms.sold_bot_raw) + int(ms.sold_external_raw) == 150
    sum_steps = sum(int(getattr(s, "sold_raw", 0) or 0) for s in ms.executed_steps.values())
    assert sum_steps == 150


def test_add_sell_accounting_external():
    """External sell increments sold_external_raw; invariant holds."""
    from mint_ladder_bot.runner import _add_sell_accounting, _ensure_sell_accounting_backfill

    steps = {
        "ext_abc": StepExecutionInfo(sig="s1", time=datetime.now(tz=timezone.utc), sold_raw="300", sol_out=0.03),
    }
    ms = _ms(executed_steps=steps)
    _ensure_sell_accounting_backfill(ms)
    assert ms.sold_bot_raw == "0"
    assert ms.sold_external_raw == "300"
    ms.executed_steps["ext_def"] = StepExecutionInfo(
        sig="s2", time=datetime.now(tz=timezone.utc), sold_raw="100", sol_out=0.01
    )
    _add_sell_accounting(ms, external_delta=100)
    assert ms.sold_bot_raw == "0"
    assert ms.sold_external_raw == "400"
    assert int(ms.sold_bot_raw) + int(ms.sold_external_raw) == 400
    sum_steps = sum(int(getattr(s, "sold_raw", 0) or 0) for s in ms.executed_steps.values())
    assert sum_steps == 400


def test_dashboard_truth_sold_bot_external():
    """dashboard_truth returns sold_bot_raw and sold_external_raw from mint_data or steps."""
    from mint_ladder_bot import dashboard_truth as dt

    mint_data = {
        "executed_steps": {
            "1": {"sold_raw": "100", "sol_out": 0.01},
            "ext_abc": {"sold_raw": "200", "sol_out": 0.02},
        },
        "lots": [],
    }
    bot = dt._sold_bot_raw_from_mint_data(mint_data)
    ext = dt._sold_external_raw_from_mint_data(mint_data)
    assert bot == 100
    assert ext == 200
    mint_data["sold_bot_raw"] = "111"
    mint_data["sold_external_raw"] = "222"
    assert dt._sold_bot_raw_from_mint_data(mint_data) == 111
    assert dt._sold_external_raw_from_mint_data(mint_data) == 222


def test_step_execution_uses_actual_sold_not_planned():
    """
    Accounting must follow actual sold amount, not planned step size.

    Simulate a fractured step where the planned amount is 1000 but only 600
    tokens are actually sold. StepExecutionInfo.sold_raw and sold_bot_raw
    must reflect 600 so that dashboard + invariants see the real execution.
    """
    from mint_ladder_bot.runner import _add_sell_accounting

    # planned step size (for context only)
    planned = 1000
    actual = 600

    steps = {
        "step1": StepExecutionInfo(
            sig="s1",
            time=datetime.now(tz=timezone.utc),
            sold_raw=str(actual),  # actual observed sold amount, not planned
            sol_out=0.01,
        )
    }
    ms = _ms(executed_steps=steps)
    # Backfill from steps then add accounting for the same actual amount.
    from mint_ladder_bot.runner import _ensure_sell_accounting_backfill

    _ensure_sell_accounting_backfill(ms)
    assert ms.sold_bot_raw == str(actual)
    assert ms.sold_external_raw == "0"

    # Another accounting increment by the same actual amount keeps invariant
    # sold_bot_raw + sold_external_raw == sum(executed_steps[].sold_raw).
    ms.executed_steps["step2"] = StepExecutionInfo(
        sig="s2",
        time=datetime.now(tz=timezone.utc),
        sold_raw=str(actual),
        sol_out=0.02,
    )
    _add_sell_accounting(ms, bot_delta=actual)
    assert ms.sold_bot_raw == str(actual * 2)
    assert ms.sold_external_raw == "0"
    sum_steps = sum(int(getattr(s, "sold_raw", 0) or 0) for s in ms.executed_steps.values())
    assert sum_steps == int(ms.sold_bot_raw) + int(ms.sold_external_raw)
