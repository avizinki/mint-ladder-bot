from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .models import RuntimeMintState, RuntimeState, StatusFile

logger = logging.getLogger(__name__)

NUM_STATE_BACKUPS = 3


class StateCorruptedError(RuntimeError):
    """Raised when state.json exists but cannot be parsed or validated safely."""


def load_state(path: Path, status_file: Path) -> RuntimeState:
    """
    Load runtime state from path.

    - If state.json is missing, rebuild minimal state from status_file (cold start).
    - If state.json exists but is malformed or fails validation, raise StateCorruptedError
      so callers can fail closed rather than silently discarding potentially important
      evidence about prior runs.
    """
    if not path.exists():
        return _fresh_state_from_status(status_file)

    try:
        raw = path.read_text()
    except Exception as exc:
        raise StateCorruptedError(f"Failed to read state.json: {exc}") from exc

    try:
        data = json.loads(raw)
        state = RuntimeState.model_validate(data)
    except Exception as exc:
        logger.error("state.json load failed; treating as corrupted: %s", exc)
        raise StateCorruptedError(f"state.json is corrupted or invalid: {exc}") from exc

    # Backfill wallet / sol for legacy state files that were created before
    # these fields existed, so the dashboard can still render them.
    if not getattr(state, "wallet", None) or getattr(state, "sol", None) is None:
        try:
            status = StatusFile.model_validate_json(status_file.read_text())
            state.wallet = status.wallet
            state.sol = status.sol
        except Exception:
            pass
    return state


def _fresh_state_from_status(status_file: Path) -> RuntimeState:
    """Build minimal runtime state from status.json (no mints)."""
    status = StatusFile.model_validate_json(status_file.read_text())
    return RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file=str(status_file),
        wallet=status.wallet,
        sol=status.sol,
        mints={},
    )


def _rotate_state_backups(path: Path) -> None:
    """
    Copy current state to .bak.1, rotate existing .bak.1 -> .bak.2 -> .bak.3 (keep 3 backups).

    Backups are always kept next to the canonical state file (which now lives
    under the centralized runtime tree via runtime_paths.get_state_path()).
    """
    if not path.exists():
        return
    bak1 = path.with_suffix(path.suffix + ".bak.1")
    bak2 = path.with_suffix(path.suffix + ".bak.2")
    bak3 = path.with_suffix(path.suffix + ".bak.3")
    if bak2.exists():
        shutil.copy2(bak2, bak3)
    if bak1.exists():
        shutil.copy2(bak1, bak2)
    shutil.copy2(path, bak1)


def validate_state_schema(state: RuntimeState) -> list[str]:
    """CEO directive §6: required fields per mint (entry_price, lots, executed_steps, failures, buybacks)."""
    errors: list[str] = []
    for mint, ms in state.mints.items():
        if not hasattr(ms, "entry_price_sol_per_token"):
            errors.append(f"mint {mint[:12]} missing entry_price")
        if not hasattr(ms, "lots"):
            errors.append(f"mint {mint[:12]} missing lots[]")
        if not hasattr(ms, "executed_steps"):
            errors.append(f"mint {mint[:12]} missing executed_steps")
        if not hasattr(ms, "failures"):
            errors.append(f"mint {mint[:12]} missing failures")
    return errors


def save_state_atomic(path: Path, state: RuntimeState) -> None:
    """Write state atomically: temp file then rename. CEO directive §6."""
    errs = validate_state_schema(state)
    if errs:
        logger.warning("State schema validation warnings (saving anyway): %s", errs[:5])
    _rotate_state_backups(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


def normalize_state_entry_from_lots(state_path: Path, status_file: Path) -> int:
    """
    Propagate lot entry to mint when mint has no valid entry. Ensures dashboard ENTRY
    resolution has mint-level data. Returns count of mints updated.
    """
    from . import dashboard_truth as dt

    state = load_state(state_path, status_file)
    updated = 0
    for mint_addr, ms in state.mints.items():
        if not hasattr(ms, "lots") or not ms.lots:
            continue
        current = getattr(ms, "entry_price_sol_per_token", None) or getattr(ms, "working_entry_price_sol_per_token", None)
        if dt.entry_price_valid(current):
            continue
        for lot in ms.lots:
            ep = getattr(lot, "entry_price_sol_per_token", None)
            if dt.entry_price_valid(ep):
                val = dt.normalize_entry_price(ep)
                if val is not None:
                    setattr(ms, "entry_price_sol_per_token", val)
                    setattr(ms, "working_entry_price_sol_per_token", val)
                    if getattr(ms, "original_entry_price_sol_per_token", None) is None:
                        setattr(ms, "original_entry_price_sol_per_token", val)
                    updated += 1
                    logger.info("STATE_ENTRY_PROPAGATED mint=%s entry=%.6e from lot", mint_addr[:12], val)
                break
    if updated:
        save_state_atomic(state_path, state)
    return updated


def ensure_mint_state(
    state: RuntimeState,
    mint: str,
    entry_price_sol_per_token: float,
    trading_bag_raw: int,
    moonbag_raw: int,
    entry_source: Optional[str] = None,
) -> RuntimeMintState:
    from .models import BootstrapInfo

    existing = state.mints.get(mint)
    if existing is not None:
        return existing
    bootstrap = BootstrapInfo(bootstrap_pending=(entry_price_sol_per_token <= 0))
    mint_state = RuntimeMintState(
        entry_price_sol_per_token=entry_price_sol_per_token,
        entry_source=entry_source,
        original_entry_price_sol_per_token=entry_price_sol_per_token,
        working_entry_price_sol_per_token=entry_price_sol_per_token,
        trading_bag_raw=str(trading_bag_raw),
        moonbag_raw=str(moonbag_raw),
        bootstrap=bootstrap,
    )
    state.mints[mint] = mint_state
    return mint_state

