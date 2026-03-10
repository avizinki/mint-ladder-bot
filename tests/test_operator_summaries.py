from __future__ import annotations

from pathlib import Path

from mint_ladder_bot.runner import _build_startup_summary, _build_cycle_summary_fields


def test_build_startup_summary_includes_required_fields(tmp_path):
    wallet = "WALLET"
    run_mode = "LIVE"
    project_runtime_dir = tmp_path / "runtime" / "projects" / "mint_ladder_bot"
    state_path = project_runtime_dir / "state.json"
    status_path = project_runtime_dir / "status.json"

    summary = _build_startup_summary(
        wallet=wallet,
        run_mode=run_mode,
        project_runtime_dir=project_runtime_dir,
        state_path=state_path,
        status_path=status_path,
        tradable_mints=5,
        bootstrap_pending_mints=2,
        paused_mints=1,
        stop_paths="STOP_PATH_1, STOP_PATH_2",
    )

    assert summary["wallet"] == wallet
    assert summary["run_mode"] == run_mode
    assert summary["project_runtime_dir"] == str(project_runtime_dir)
    assert summary["state_file"] == str(state_path.resolve())
    assert summary["status_file"] == str(status_path.resolve())
    assert summary["tradable_mints"] == 5
    assert summary["bootstrap_pending_mints"] == 2
    assert summary["paused_mints"] == 1
    assert "STOP_PATH_1" in summary["stop_paths"]


def test_build_cycle_summary_fields_includes_trading_disabled():
    summary = _build_cycle_summary_fields(
        cycle=10,
        cycle_duration_ms=1234.0,
        rpc_latency_ms=200.0,
        sells_ok=3,
        sells_fail=1,
        buybacks_ok=0,
        buybacks_fail=0,
        paused_mints=2,
        liquidity_skips=1,
        no_step=0,
        price_none=0,
        below_target=0,
        hourcap_skip=0,
        min_trade_skip=0,
        display_pending=0,
        trading_disabled=True,
    )

    assert summary["cycle"] == 10
    assert summary["cycle_duration_ms"] == 1234.0
    assert summary["rpc_latency_ms"] == 200.0
    assert summary["sells_ok"] == 3
    assert summary["sells_fail"] == 1
    assert summary["paused_mints"] == 2
    assert summary["trading_disabled"] is True

