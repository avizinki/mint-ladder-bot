from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import runtime_paths


logger = logging.getLogger(__name__)


class DuplicateRunnerError(RuntimeError):
    """Raised when a second live runner attempts to acquire the runtime lock."""


@dataclass
class LockHandle:
    path: Path
    pid: int


def _lock_path() -> Path:
    """Canonical lock path scoped to the project runtime directory."""
    project_dir = runtime_paths.get_project_runtime_dir()
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir / "runner.lock"


def _pid_alive(pid: int) -> bool:
    """Best-effort check whether a PID is currently alive on this host."""
    if pid <= 0:
        return False
    try:
        # os.kill(pid, 0) does not actually kill; it raises if the PID does not exist.
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we may not have permission; treat as alive.
        return True
    except OSError:
        # Conservatively assume not alive on other OS errors.
        return False
    else:
        return True


def _read_lock(path: Path) -> Optional[dict]:
    """Read and parse an existing lock file. Returns None on any error."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def acquire_runtime_lock(wallet: Optional[str] = None) -> LockHandle:
    """
    Acquire the single-run lock for this project's runtime.

    - If no lock exists, create one and return a handle.
    - If a lock exists and the recorded PID is alive, raise DuplicateRunnerError.
    - If a lock exists but is stale or malformed, log a warning and replace it.
    """
    path = _lock_path()
    runtime_root = runtime_paths.get_runtime_root()
    project_dir = runtime_paths.get_project_runtime_dir()
    now = datetime.now(tz=timezone.utc).isoformat()
    pid = os.getpid()
    host = socket.gethostname()

    if path.exists():
        existing = _read_lock(path)
        existing_pid: Optional[int] = None
        if isinstance(existing, dict):
            try:
                existing_pid = int(existing.get("pid"))
            except Exception:
                existing_pid = None

        if existing_pid is not None and _pid_alive(existing_pid):
            # Another live runner holds the lock: hard fail.
            existing_wallet = (existing or {}).get("wallet") if isinstance(existing, dict) else None
            msg = (
                f"mint-ladder-bot runtime already locked by PID {existing_pid} on host {host}.\n"
                f"  lock_file: {path}\n"
                f"  lock_wallet: {existing_wallet or '(unknown)'}\n"
                f"  runtime_root: {runtime_root}\n"
                f"  project_runtime_dir: {project_dir}\n"
                "Refusing to start a second runner for the same runtime. "
                "Stop the existing process or clear the lock file manually if it is stale."
            )
            raise DuplicateRunnerError(msg)

        # Stale or malformed lock: replace it, but be explicit in logs.
        logger.warning(
            "Stale or malformed runner lock detected at %s; replacing. "
            "existing_pid=%s existing_data=%s",
            path,
            existing_pid,
            (existing if isinstance(existing, dict) else "<unparseable>"),
        )

    data = {
        "pid": pid,
        "wallet": wallet or "",
        "runtime_root": str(runtime_root),
        "project_runtime_dir": str(project_dir),
        "host": host,
        "created_at": now,
    }
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        # If we cannot write the lock, fail fast rather than running unlocked.
        raise RuntimeError(f"Failed to write runtime lock at {path}: {exc}") from exc

    return LockHandle(path=path, pid=pid)


def release_runtime_lock(handle: LockHandle) -> None:
    """
    Best-effort lock release.

    - If the lock file exists and belongs to this PID, remove it.
    - If it does not exist or belongs to a different PID, do nothing.
    - Never raises; logs warnings instead.
    """
    path = handle.path
    if not path.exists():
        return
    try:
        data = _read_lock(path)
        if not isinstance(data, dict):
            # Malformed; remove to avoid confusing future runs.
            path.unlink(missing_ok=True)
            return
        current_pid = None
        try:
            current_pid = int(data.get("pid"))
        except Exception:
            current_pid = None
        if current_pid == handle.pid or current_pid is None:
            path.unlink(missing_ok=True)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to release runtime lock at %s: %s", path, exc)

