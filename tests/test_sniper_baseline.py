from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mint_ladder_bot.dashboard_server import build_dashboard_payload
from mint_ladder_bot.models import RuntimeState, SolBalance
from mint_ladder_bot.state import load_state, save_state_atomic


def test_runtime_state_backward_compat_without_sniper_fields(tmp_path: Path) -> None:
    """Old state.json without sniper fields should still load via RuntimeState model."""
    state_path = tmp_path / "state.json"
    status_path = tmp_path / "status.json"
    # Minimal legacy-like state (no sniper fields).
    legacy = RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file=str(status_path),
        wallet="WALLET_OK",
        sol=SolBalance(lamports=0, sol=0.0),
        mints={},
    )
    state_path.write_text(legacy.model_dump_json(indent=2), encoding="utf-8")
    # load_state uses RuntimeState.model_validate under the hood
    loaded = RuntimeState.model_validate_json(state_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, RuntimeState)
    # Sniper fields should exist with defaults even if absent on disk.
    assert hasattr(loaded, "sniper_manual_seed_queue")
    assert loaded.sniper_manual_seed_queue == []


def test_runtime_state_round_trip_with_sniper_defaults(tmp_path: Path) -> None:
    """RuntimeState with default sniper fields should serialize/deserialize cleanly."""
    state_path = tmp_path / "state.json"
    status_path = tmp_path / "status.json"
    state = RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file=str(status_path),
        wallet="WALLET_OK",
        sol=SolBalance(lamports=0, sol=0.0),
        mints={},
    )
    save_state_atomic(state_path, state)
    reloaded = load_state(state_path, status_path)
    assert isinstance(reloaded, RuntimeState)
    assert reloaded.sniper_manual_seed_queue == []
    assert reloaded.sniper_pending_attempts == {}


def test_dashboard_payload_includes_sniper_sections_when_empty(tmp_path: Path) -> None:
    """Dashboard payload should always include sniper sections, even with no state files."""
    data_dir = tmp_path
    payload = build_dashboard_payload(data_dir)
    assert "sniper_summary" in payload
    assert "sniper_pending_attempts" in payload
    assert "sniper_recent_decisions" in payload
    summary = payload["sniper_summary"]
    assert summary.get("enabled") is False
    assert summary.get("mode") == "disabled"
