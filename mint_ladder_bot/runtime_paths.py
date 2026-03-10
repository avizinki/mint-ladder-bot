from __future__ import annotations

import os
from pathlib import Path


def _repo_root() -> Path:
    """
    Monorepo root.

    Assumes this file lives in projects/mint-ladder-bot/mint_ladder_bot/
    or mint-ladder-bot/mint_ladder_bot/ and walks up accordingly.
    """
    here = Path(__file__).resolve()
    pkg_root = here.parent  # .../mint_ladder_bot
    project_root = pkg_root.parent
    # When used from the standalone project, repo root == project_root.
    # When used from monorepo (projects/mint-ladder-bot), repo root == project_root.parent.
    if (project_root / "pyproject.toml").exists():
        return project_root
    return project_root.parent


def get_runtime_root() -> Path:
    """
    Canonical runtime root for all generated artifacts.

    Resolution order:
    - RUNTIME_ROOT env var (containers: e.g. /app/runtime)
    - <repo_root>/runtime
    """
    env_root = os.getenv("RUNTIME_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return (_repo_root() / "runtime").resolve()


def get_project_runtime_dir() -> Path:
    """
    Runtime directory for mint-ladder-bot.

    <runtime_root>/projects/mint_ladder_bot
    """
    return get_runtime_root() / "projects" / "mint_ladder_bot"


def get_project_log_dir() -> Path:
    """
    Log directory for mint-ladder-bot.

    <runtime_root>/logs/mint-ladder-bot
    """
    return get_runtime_root() / "logs" / "mint-ladder-bot"


def get_state_path() -> Path:
    """Canonical path to state.json for the primary wallet."""
    return get_project_runtime_dir() / "state.json"


def get_status_path() -> Path:
    """Canonical path to status.json for the primary wallet."""
    return get_project_runtime_dir() / "status.json"


def get_events_path() -> Path:
    """Canonical path to events.jsonl for the primary wallet."""
    return get_project_runtime_dir() / "events.jsonl"


def get_health_status_path() -> Path:
    """Canonical path to health_status.json for the primary wallet runtime."""
    return get_project_runtime_dir() / "health_status.json"


def get_run_log_path() -> Path:
    """Canonical path to run.log for the primary wallet runtime."""
    return get_project_log_dir() / "run.log"

