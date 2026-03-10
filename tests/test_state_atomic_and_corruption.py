from __future__ import annotations

import json
from pathlib import Path

import pytest

from datetime import datetime, timezone

from mint_ladder_bot.models import RuntimeState, SolBalance, StatusFile, RpcInfo
from mint_ladder_bot.state import (
    StateCorruptedError,
    load_state,
    save_state_atomic,
)


def _write_status(tmp_path: Path) -> Path:
    status_path = tmp_path / "status.json"
    status = StatusFile(
        version=1,
        created_at=datetime.now(tz=timezone.utc),
        wallet="WALLET",
        rpc=RpcInfo(endpoint="http://localhost", latency_ms=None),
        sol=SolBalance(lamports=0, sol=0.0),
        mints=[],
    )
    status_path.write_text(status.model_dump_json(indent=2), encoding="utf-8")
    return status_path


def test_save_state_atomic_replaces_file_and_rotates_backups(tmp_path):
    state_path = tmp_path / "state.json"
    status_path = _write_status(tmp_path)

    # Initial state.
    state1 = RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file=str(status_path),
        wallet="WALLET",
        sol=SolBalance(lamports=0, sol=0.0),
        mints={},
    )
    save_state_atomic(state_path, state1)
    original_content = state_path.read_text(encoding="utf-8")

    # Second state; should rotate backups and replace contents.
    state2 = RuntimeState(
        version=2,
        started_at=datetime.now(tz=timezone.utc),
        status_file=str(status_path),
        wallet="WALLET",
        sol=SolBalance(lamports=1, sol=1.0),
        mints={},
    )
    save_state_atomic(state_path, state2)
    new_content = state_path.read_text(encoding="utf-8")

    assert '"version": 2' in new_content
    assert '"version": 1' in (state_path.with_suffix(".json.bak.1")).read_text(encoding="utf-8")
    # Ensure temp file is not left behind.
    assert not state_path.with_suffix(".json.tmp").exists()


def test_load_state_missing_builds_fresh_from_status(tmp_path):
    state_path = tmp_path / "state.json"
    status_path = _write_status(tmp_path)

    assert not state_path.exists()
    state = load_state(state_path, status_path)
    assert state.wallet == "WALLET"


def test_load_state_raises_on_malformed_json(tmp_path):
    state_path = tmp_path / "state.json"
    status_path = _write_status(tmp_path)

    state_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(StateCorruptedError):
        _ = load_state(state_path, status_path)


def test_load_state_raises_on_truncated_json(tmp_path):
    state_path = tmp_path / "state.json"
    status_path = _write_status(tmp_path)

    # Write a truncated but syntactically plausible prefix.
    state_path.write_text('{"version": 1, "wallet": "WALLET"', encoding="utf-8")

    with pytest.raises(StateCorruptedError):
        _ = load_state(state_path, status_path)

