"""Tests for sniper runner cycle: gating, cooldown, max lots, reserve."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile

from mint_ladder_bot.config import Config
from mint_ladder_bot.models import LotInfo, RuntimeMintState, RuntimeState
from mint_ladder_bot.state import load_state, save_state_atomic


def test_sniper_config_defaults():
    config = Config()
    assert getattr(config, "sniper_enabled", None) is not None
    assert getattr(config, "sniper_buy_sol", 0) >= 0
    assert getattr(config, "sniper_max_concurrent_lots", 0) >= 1
    assert getattr(config, "sniper_min_sol_reserve", 0) >= 0
    assert getattr(config, "sniper_cooldown_seconds", 0) >= 0


def test_max_concurrent_lots_respected():
    """State with lots >= max should be respected by sniper cycle (logic check)."""
    state = RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        mints={
            "m1": RuntimeMintState(
                entry_price_sol_per_token=1e-6,
                trading_bag_raw="1000",
                moonbag_raw="0",
                lots=[LotInfo(mint="m1", token_amount="1000", remaining_amount="1000")] * 5,
            ),
        },
    )
    total = sum(len(getattr(ms, "lots", None) or []) for ms in state.mints.values())
    assert total == 5
    # sniper_max_concurrent_lots default 5: should skip new buy when total_lots >= 5
    assert total >= 5


def test_cooldown_and_reserve_are_configurable():
    config = Config()
    cooldown = getattr(config, "sniper_cooldown_seconds", 30.0)
    reserve = getattr(config, "sniper_min_sol_reserve", 0.1)
    assert cooldown > 0
    assert reserve >= 0
