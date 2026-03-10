from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from mint_ladder_bot.config import Config
from mint_ladder_bot.runner import _handle_rpc_failure, _compute_trading_disabled


class _DummyConfig(Config):
    def __init__(self) -> None:
        super().__init__()
        self.rpc_failures_threshold = 3
        self.rpc_cooldown_sec = 60
        self.trading_enabled = True
        self.live_trading = True
        self.trading_disabled_env = False


def test_handle_rpc_failure_escalates_to_global_pause(tmp_path):
    cfg = _DummyConfig()
    run_state: Dict[str, Any] = {
        "global_trading_paused_until": None,
        "rpc_failures_consecutive": 0,
    }
    journal = tmp_path / "events.jsonl"

    # First failure: counter increments, no pause yet.
    _handle_rpc_failure(run_state, cfg, journal)
    assert run_state["rpc_failures_consecutive"] == 1
    assert run_state["global_trading_paused_until"] is None

    # Second failure: still below threshold.
    _handle_rpc_failure(run_state, cfg, journal)
    assert run_state["rpc_failures_consecutive"] == 2
    assert run_state["global_trading_paused_until"] is None

    # Third failure: threshold reached; trading should be paused globally.
    _handle_rpc_failure(run_state, cfg, journal)
    assert run_state["rpc_failures_consecutive"] == 3
    paused_until = run_state.get("global_trading_paused_until")
    assert paused_until is not None
    assert isinstance(paused_until, datetime)


def test_compute_trading_disabled_respects_global_pause():
    cfg = _DummyConfig()
    now = datetime.now(tz=timezone.utc)
    future = now + timedelta(seconds=30)

    # No STOP, trading flags enabled, no pause -> trading allowed.
    disabled = _compute_trading_disabled(
        config=cfg,
        stop_active=False,
        global_pause_until=None,
        now_utc=now,
    )
    assert disabled is False

    # Global pause in the future -> trading disabled (degraded mode).
    disabled = _compute_trading_disabled(
        config=cfg,
        stop_active=False,
        global_pause_until=future,
        now_utc=now,
    )
    assert disabled is True

