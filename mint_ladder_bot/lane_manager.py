"""
Lane assignment layer for mint-ladder-bot manager runtime.

Maps (wallet_id, lane_id) to assignment records (enabled, lane_paused, lane_cooldown_until)
and provides get_eligible_lanes(wallet_state_source) for the manager cycle.
Wallet state is read from an external source (callable or interface); this module
does not implement wallet state storage.

Specs: docs/trading/mint-ladder-bot-lane-assignment-model.md,
       docs/trading/mint-ladder-bot-manager-runtime.md.
Task: docs/trading-contract-T11.md.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

# Wallet state machine: only "active" allows execution (docs/trading/mint-ladder-bot-wallet-state-machine.md).
ACTIVE = "active"


@dataclass
class LaneAssignment:
    """Per (wallet_id, lane_id) record: enabled, lane_paused, lane_cooldown_until."""

    wallet_id: str
    lane_id: str
    enabled: bool = True
    lane_paused: bool = False
    lane_cooldown_until: Optional[float] = None  # Unix timestamp; None = no cooldown


# Wallet state source: callable(wallet_id) -> state string, or object with get_state(wallet_id).
WalletStateSource = Union[Callable[[str], str], "WalletStateProtocol"]


class WalletStateProtocol:
    """Small interface for wallet state: get_state(wallet_id) -> 'active'|'paused'|..."""

    def get_state(self, wallet_id: str) -> str:
        ...


def _resolve_wallet_state(source: WalletStateSource, wallet_id: str) -> str:
    if callable(source):
        return source(wallet_id)
    return source.get_state(wallet_id)


class LaneManager:
    """
    Holds lane assignments (wallet_id, lane_id) with enabled, lane_paused, lane_cooldown_until.
    Loads from dict or file-backed JSON; provides get_eligible_lanes(wallet_state_source).
    """

    def __init__(self, assignments: Optional[Dict[Tuple[str, str], LaneAssignment]] = None):
        self._assignments: Dict[Tuple[str, str], LaneAssignment] = assignments or {}

    def _key(self, wallet_id: str, lane_id: str) -> Tuple[str, str]:
        return (wallet_id, lane_id)

    def get_assignment(self, wallet_id: str, lane_id: str) -> Optional[LaneAssignment]:
        return self._assignments.get(self._key(wallet_id, lane_id))

    def get_eligible_lanes(
        self,
        wallet_state_source: WalletStateSource,
        now: Optional[float] = None,
    ) -> List[Tuple[str, str]]:
        """
        Return list of (wallet_id, lane_id) where:
        - wallet state is active,
        - assignment exists and is enabled,
        - lane is not paused,
        - lane cooldown has expired (or not set).
        """
        if now is None:
            now = time.time()
        out: List[Tuple[str, str]] = []
        for (wallet_id, lane_id), rec in list(self._assignments.items()):
            if not rec.enabled:
                continue
            if rec.lane_paused:
                continue
            if rec.lane_cooldown_until is not None and now < rec.lane_cooldown_until:
                continue
            if _resolve_wallet_state(wallet_state_source, wallet_id) != ACTIVE:
                continue
            out.append((wallet_id, lane_id))
        return out

    def enable_lane(self, wallet_id: str, lane_id: str) -> None:
        key = self._key(wallet_id, lane_id)
        if key in self._assignments:
            self._assignments[key].enabled = True
        else:
            self._assignments[key] = LaneAssignment(wallet_id=wallet_id, lane_id=lane_id, enabled=True)

    def disable_lane(self, wallet_id: str, lane_id: str) -> None:
        key = self._key(wallet_id, lane_id)
        if key in self._assignments:
            self._assignments[key].enabled = False
        else:
            self._assignments[key] = LaneAssignment(
                wallet_id=wallet_id, lane_id=lane_id, enabled=False
            )

    def set_lane_pause(self, wallet_id: str, lane_id: str, paused: bool) -> None:
        key = self._key(wallet_id, lane_id)
        if key in self._assignments:
            self._assignments[key].lane_paused = paused
        else:
            self._assignments[key] = LaneAssignment(
                wallet_id=wallet_id, lane_id=lane_id, lane_paused=paused
            )

    def set_lane_cooldown(self, wallet_id: str, lane_id: str, until_ts: Optional[float]) -> None:
        key = self._key(wallet_id, lane_id)
        if key in self._assignments:
            self._assignments[key].lane_cooldown_until = until_ts
        else:
            self._assignments[key] = LaneAssignment(
                wallet_id=wallet_id, lane_id=lane_id, lane_cooldown_until=until_ts
            )

    def load_from_dict(self, data: List[dict]) -> None:
        """Load assignments from a list of dicts (e.g. from JSON). Merges into current store."""
        for d in data:
            w = d.get("wallet_id")
            l = d.get("lane_id")
            if w is None or l is None:
                continue
            key = (str(w), str(l))
            self._assignments[key] = LaneAssignment(
                wallet_id=str(w),
                lane_id=str(l),
                enabled=d.get("enabled", True),
                lane_paused=d.get("lane_paused", False),
                lane_cooldown_until=d.get("lane_cooldown_until"),
            )

    def load_from_file(self, path: Union[str, Path]) -> None:
        """Load assignments from a JSON file. File must contain a list of assignment objects."""
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            self.load_from_dict(data)
        else:
            self.load_from_dict([])

    def merge_persisted_state(self, data: List[dict]) -> None:
        """
        Overlay persisted state onto existing assignments. Only updates keys that already
        exist; does not add new (wallet_id, lane_id). Used to restore lane_cooldown_until,
        lane_paused, enabled from a previous run without overwriting the assignment set.
        """
        for d in data:
            w = d.get("wallet_id")
            l = d.get("lane_id")
            if w is None or l is None:
                continue
            key = (str(w), str(l))
            if key not in self._assignments:
                continue
            rec = self._assignments[key]
            if "enabled" in d:
                rec.enabled = bool(d["enabled"])
            if "lane_paused" in d:
                rec.lane_paused = bool(d["lane_paused"])
            if "lane_cooldown_until" in d:
                rec.lane_cooldown_until = d["lane_cooldown_until"]

    def save_to_file(self, path: Union[str, Path]) -> None:
        """Persist current assignments to a JSON file."""
        data = [
            {
                "wallet_id": a.wallet_id,
                "lane_id": a.lane_id,
                "enabled": a.enabled,
                "lane_paused": a.lane_paused,
                "lane_cooldown_until": a.lane_cooldown_until,
            }
            for a in self._assignments.values()
        ]
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
