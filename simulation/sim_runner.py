"""
Simulation runner for mint-ladder-bot multi-wallet harness.

Loads sim wallet config, provides a wallet state source from config, runs
lane_manager.get_eligible_lanes, and reports eligible (wallet_id, lane_id).
No execution, no network, no signing. STOP and other guards are simulated
via in-memory flags.

Spec: docs/trading/mint-ladder-bot-multiwallet-simulation.md
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Allow importing mint_ladder_bot when run as script (e.g. python simulation/sim_runner.py).
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from mint_ladder_bot.lane_manager import LaneManager, LaneAssignment

# --- Simulated guard: STOP flag (in-memory, no file I/O) ---
# For testing: set SIM_STOP=1 in env or set stop_flag_active = True in code.
# RPC failure count, 24h cap, and other guards can be injected in a future iteration;
# for T13 we only simulate STOP and ensure no network calls and no signing.
_STOP_FLAG_ACTIVE: bool = os.environ.get("SIM_STOP", "").strip() in ("1", "true", "True")


def get_stop_flag_active() -> bool:
    """Return whether the simulated STOP guard is active (trading disabled)."""
    return _STOP_FLAG_ACTIVE


def set_stop_flag_active(value: bool) -> None:
    """Set the in-memory STOP flag for tests. No file I/O."""
    global _STOP_FLAG_ACTIVE
    _STOP_FLAG_ACTIVE = value


class SimWalletStateSource:
    """Wallet state source driven from loaded sim_wallets config. No real keys."""

    def __init__(self, wallet_states: Dict[str, str]):
        self._states = dict(wallet_states)

    def get_state(self, wallet_id: str) -> str:
        return self._states.get(wallet_id, "disabled")


def load_sim_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load simulation config from JSON. Default: simulation/sim_wallets.json next to this file."""
    if path is None:
        path = Path(__file__).resolve().parent / "sim_wallets.json"
    with open(path, "r") as f:
        return json.load(f)


def build_wallet_state_source(config: Dict[str, Any]) -> SimWalletStateSource:
    """Build wallet state source from config['wallets'] (list of {wallet_id, state})."""
    wallets = config.get("wallets") or []
    states = {str(w["wallet_id"]): str(w.get("state", "disabled")) for w in wallets}
    return SimWalletStateSource(states)


def build_lane_manager(config: Dict[str, Any]) -> LaneManager:
    """Build LaneManager from config['lane_assignments'] if present."""
    manager = LaneManager()
    assignments = config.get("lane_assignments")
    if assignments:
        manager.load_from_dict(assignments)
    return manager


def run_one_shot(config_path: Optional[Path] = None) -> List[Tuple[str, str]]:
    """
    One-shot mode: load config, build wallet state source, get eligible lanes, report.
    No execution, no network, no signing.
    Returns list of (wallet_id, lane_id) that are eligible.
    """
    config = load_sim_config(config_path)
    wallet_state_source = build_wallet_state_source(config)
    lane_manager = build_lane_manager(config)
    eligible = lane_manager.get_eligible_lanes(wallet_state_source)

    stop_active = get_stop_flag_active()
    if stop_active:
        print("SIM: STOP flag active (trading disabled)")
    print("Eligible (wallet_id, lane_id):", eligible)
    return eligible


def main() -> None:
    run_one_shot()


if __name__ == "__main__":
    main()
