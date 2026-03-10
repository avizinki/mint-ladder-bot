from __future__ import annotations

from datetime import datetime, timezone

from mint_ladder_bot.config import Config
from mint_ladder_bot.runner import _build_health_runtime_info


class _DummyConfig(Config):
    def __init__(self) -> None:
        super().__init__()
        self.rpc_endpoint = "https://mainnet.helius-rpc.com"
        self.buyback_enabled = False


def test_health_runtime_info_contains_minimal_heartbeat_fields(monkeypatch):
    cfg = _DummyConfig()
    info = _build_health_runtime_info(
        cycle=5,
        rpc_latency_ms=123.4,
        paused_mints=2,
        clean_start=False,
        backfill_completed=True,
        config=cfg,
        sell_readiness={"MINT": {"reason": "ok"}},
        monitor_only=False,
        trading_ok=True,
        last_error=None,
        rpc_failures_consecutive=2,
        global_trading_paused_until=None,
        cycle_mismatch_first_detected_at_cycle=7,
        sells_failed=3,
    )

    # Minimal heartbeat/observability fields.
    assert info["cycles"] == 5
    assert info["rpc_latency_ms"] == 123.4
    assert info["paused_mints"] == 2
    assert info["current_cycle_number"] == 5
    assert info["clean_start_active"] is False
    assert info["backfill_completed"] is True
    assert info["rpc_provider_label"] == "helius"
    assert info["buyback_enabled"] is False
    assert info["sell_readiness"] == {"MINT": {"reason": "ok"}}
    assert info["runner_mode"] == "live"
    assert info["process_state"] == "running"
    assert "loop_heartbeat_at" in info
    assert "last_successful_cycle_at" in info
    assert "last_failed_cycle_at" in info
    assert "last_error" in info
    # Anomaly counters
    assert info["rpc_failures_consecutive"] == 2
    assert info["global_trading_paused_until"] is None
    assert info["cycle_mismatch_first_detected_at_cycle"] == 7
    assert info["sells_failed"] == 3

