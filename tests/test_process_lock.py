from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mint_ladder_bot import runtime_paths
from mint_ladder_bot.process_lock import (
    DuplicateRunnerError,
    LockHandle,
    acquire_runtime_lock,
    release_runtime_lock,
)


def _configure_runtime_paths(monkeypatch, tmp_path: Path) -> Path:
    """
    Point runtime_paths at a temporary runtime root so locks are fully isolated for tests.
    """
    runtime_root = tmp_path / "runtime"
    project_dir = runtime_root / "projects" / "mint_ladder_bot"
    project_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(runtime_paths, "get_runtime_root", lambda: runtime_root)
    monkeypatch.setattr(runtime_paths, "get_project_runtime_dir", lambda: project_dir)
    return project_dir


def test_first_acquire_succeeds_and_writes_lock(monkeypatch, tmp_path):
    project_dir = _configure_runtime_paths(monkeypatch, tmp_path)

    handle = acquire_runtime_lock(wallet="WALLET_PUBKEY")
    lock_path = project_dir / "runner.lock"
    assert handle.path == lock_path
    assert lock_path.exists()

    data = json.loads(lock_path.read_text())
    assert data["pid"] == os.getpid()
    assert data["wallet"] == "WALLET_PUBKEY"
    assert data["project_runtime_dir"] == str(project_dir)


def test_second_acquire_with_live_pid_fails(monkeypatch, tmp_path):
    project_dir = _configure_runtime_paths(monkeypatch, tmp_path)

    # First acquire should succeed.
    handle1 = acquire_runtime_lock(wallet="WALLET_ONE")
    assert handle1.path.exists()

    # Second acquire in the same process should see live PID and raise DuplicateRunnerError.
    with pytest.raises(DuplicateRunnerError):
        acquire_runtime_lock(wallet="WALLET_TWO")

    # Lock file should still belong to the original wallet.
    data = json.loads((project_dir / "runner.lock").read_text())
    assert data["wallet"] == "WALLET_ONE"


def test_stale_lock_recognized_and_replaced(monkeypatch, tmp_path):
    project_dir = _configure_runtime_paths(monkeypatch, tmp_path)
    lock_path = project_dir / "runner.lock"

    # Write a stale lock with an impossible PID.
    stale = {
        "pid": 999999999,
        "wallet": "OLD_WALLET",
        "project_runtime_dir": str(project_dir),
    }
    lock_path.write_text(json.dumps(stale), encoding="utf-8")

    handle = acquire_runtime_lock(wallet="NEW_WALLET")
    data = json.loads(lock_path.read_text())
    assert data["pid"] == handle.pid
    assert data["wallet"] == "NEW_WALLET"


def test_release_removes_lock(monkeypatch, tmp_path):
    project_dir = _configure_runtime_paths(monkeypatch, tmp_path)

    handle = acquire_runtime_lock(wallet="WALLET")
    lock_path = project_dir / "runner.lock"
    assert lock_path.exists()

    release_runtime_lock(handle)
    assert not lock_path.exists()


def test_malformed_lock_treated_safely(monkeypatch, tmp_path):
    project_dir = _configure_runtime_paths(monkeypatch, tmp_path)
    lock_path = project_dir / "runner.lock"

    # Write a malformed, non-JSON lock file.
    lock_path.write_text("not-json", encoding="utf-8")

    # acquire_runtime_lock should not crash; it should replace the malformed lock.
    handle = acquire_runtime_lock(wallet="WALLET")
    data = json.loads(lock_path.read_text())
    assert data["pid"] == handle.pid
    assert data["wallet"] == "WALLET"

