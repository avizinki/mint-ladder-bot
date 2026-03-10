from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mint_ladder_bot.config import Config
from mint_ladder_bot.models import RuntimeMintState
from mint_ladder_bot.runner import (
    _compute_trading_disabled,
    _pre_swap_invariants_ok,
)
from mint_ladder_bot.strategy import LadderStep


class _DummyConfig(Config):
    def __init__(self) -> None:
        super().__init__()
        self.trading_enabled = True
        self.live_trading = True
        self.trading_disabled_env = False


def test_compute_trading_disabled_respects_stop_only():
    cfg = _DummyConfig()
    now = datetime.now(tz=timezone.utc)

    # No STOP, no global pause -> trading allowed.
    disabled = _compute_trading_disabled(
        config=cfg,
        stop_active=False,
        global_pause_until=None,
        now_utc=now,
    )
    assert disabled is False

    # STOP active blocks trading regardless of other flags.
    disabled = _compute_trading_disabled(
        config=cfg,
        stop_active=True,
        global_pause_until=None,
        now_utc=now,
    )
    assert disabled is True


def test_pre_swap_invariants_respects_trading_disabled():
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw="1000000",
        moonbag_raw="0",
        lots=[],
    )
    step = LadderStep(step_id=1, multiple=1.1, target_price_sol_per_token=1e-6 * 1.1, sell_amount_raw=100_000)
    ok, reason = _pre_swap_invariants_ok(
        mint_state=ms,
        step_key="step_1",
        step=step,
        trading_bag_raw=1_000_000,
        liq_cap_raw=None,
        trading_disabled=True,
        quote_ts=0.0,
        quote_max_age_sec=60.0,
    )
    assert ok is False
    assert reason == "trading_disabled"

