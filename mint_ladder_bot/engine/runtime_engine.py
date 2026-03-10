"""
Multi-strategy runtime engine for mint-ladder-bot.

Orchestrates cycles: eligible (wallet_id, lane_id) pairs, risk gates, one strategy
step or stub per pair, and monitoring events. Preserves T7 risk guards and T9 wallet
isolation.

Architecture: docs/trading/mint-ladder-bot-engine-architecture.md.
Contract: docs/trading-contract-T14.md.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, Union

# Optional execution hook: (wallet_id, lane_id) -> outcome string or any.
RunOneLaneCallable = Optional[Callable[[str, str], Any]]

from ..lane_manager import ACTIVE, LaneManager, WalletStateSource, _resolve_wallet_state


# Kill-switch filename for pre-run stub when state_path is used (aligned with runner).
STOP_FILE = "STOP"


def _default_pre_run_risk_check(_wallet_id: str, _lane_id: str) -> Tuple[bool, Optional[str]]:
    """Stub: no guard blocks. Caller can inject real guard logic via pre_run_risk_check."""
    return True, None


def _stop_file_present(state_path: Path) -> bool:
    """True if STOP file exists at cwd or state_path.parent (aligned with runner)."""
    cwd = Path.cwd()
    if (cwd / STOP_FILE).exists():
        return True
    if (state_path.parent / STOP_FILE).exists():
        return True
    return False


def _global_pause_active(get_run_state: Callable[[], dict]) -> bool:
    """True if global_trading_paused_until is set and now < until."""
    state = get_run_state()
    until = state.get("global_trading_paused_until")
    if until is None:
        return False
    now = time.time()
    if isinstance(until, (int, float)):
        return now < until
    # datetime-like: assume has timestamp() or convert
    if hasattr(until, "timestamp"):
        return now < until.timestamp()
    return False


class StrategyRegistryProtocol(Protocol):
    """Protocol for strategy registry: get_strategy(lane_id) -> strategy or None."""

    def get_strategy(self, lane_id: str) -> Optional[Any]:
        ...


class WalletManagerProtocol(Protocol):
    """Protocol for wallet resolution: identity and keypair by wallet_id only."""

    def resolve_identity(self, wallet_id: str) -> Any:
        ...

    def resolve_keypair(self, wallet_id: str) -> Any:
        ...


def _get_strategy(
    registry: Union[Dict[str, Any], StrategyRegistryProtocol],
    lane_id: str,
) -> Optional[Any]:
    """Resolve strategy for lane_id from dict or registry object."""
    if isinstance(registry, dict):
        return registry.get(lane_id)
    return registry.get_strategy(lane_id)


def _resolve_wallet_state_for_engine(source: WalletStateSource, wallet_id: str) -> str:
    """Resolve wallet state from callable or WalletStateProtocol."""
    return _resolve_wallet_state(source, wallet_id)


class RuntimeEngine:
    """
    Multi-strategy runtime: runs cycles over eligible (wallet_id, lane_id) pairs,
    enforces pre/post risk gates, runs one strategy step or no-op stub per pair,
    emits monitoring events. Wallet isolation: only wallet_id is passed to
    wallet_manager; no cross-wallet state.
    """

    def __init__(
        self,
        wallet_state_source: WalletStateSource,
        strategy_registry: Union[Dict[str, Any], StrategyRegistryProtocol],
        lane_manager: Optional[LaneManager] = None,
        wallet_manager: Optional[Union[WalletManagerProtocol, Any]] = None,
        *,
        assignment_config: Optional[List[dict]] = None,
        state_path: Optional[Union[str, Path]] = None,
        get_run_state: Optional[Callable[[], dict]] = None,
        pre_run_risk_check: Optional[
            Callable[[str, str], Tuple[bool, Optional[str]]]
        ] = None,
        run_one_lane: RunOneLaneCallable = None,
        event_callback: Optional[Callable[[dict], None]] = None,
        events: Optional[List[dict]] = None,
    ) -> None:
        """
        Args:
            wallet_state_source: Callable(wallet_id) -> state or get_state(wallet_id).
            strategy_registry: Dict[lane_id, strategy] or object with get_strategy(lane_id).
            lane_manager: Supplies eligible (wallet_id, lane_id) via get_eligible_lanes.
                If None, assignment_config must be provided and a LaneManager is built from it.
            wallet_manager: Optional. resolve_identity(wallet_id), resolve_keypair(wallet_id).
                Can be None (stubbed; no resolution).
            assignment_config: If lane_manager is None, build LaneManager from this list.
                If lane_manager is provided, load this into it (merge).
            state_path: Optional. If set, pre-run STOP check uses this path (cwd + state_path.parent).
            get_run_state: Optional. Callable returning dict with global_trading_paused_until.
                Used for pre-run RPC pause check when pre_run_risk_check is not provided.
            pre_run_risk_check: Optional. (wallet_id, lane_id) -> (allowed, guard_type).
                If any guard blocks, return (False, guard_type). If None, engine uses
                internal stub (state_path STOP + get_run_state global pause when provided).
            run_one_lane: Optional. When set, called for each (wallet_id, lane_id) instead
                of _run_one_step_stub; (wallet_id, lane_id) -> outcome (e.g. "dry_run", "stubbed").
            event_callback: Optional. Called with each event dict.
            events: Optional. List to append event dicts to (mutated in run_cycle).
        """
        if lane_manager is None and assignment_config is None:
            raise ValueError("Either lane_manager or assignment_config must be provided")
        if lane_manager is not None:
            self._lane_manager = lane_manager
            if assignment_config is not None:
                lane_manager.load_from_dict(assignment_config)
        else:
            self._lane_manager = build_lane_manager_from_config(assignment_config or [])

        self._wallet_state_source = wallet_state_source
        self._strategy_registry = strategy_registry
        self._wallet_manager = wallet_manager
        self._state_path = Path(state_path) if state_path else None
        self._get_run_state = get_run_state
        self._pre_run_risk_check = pre_run_risk_check
        self._run_one_lane = run_one_lane
        self._event_callback = event_callback
        self._events: List[dict] = events if events is not None else []

    def _emit(self, event: dict) -> None:
        event["timestamp"] = time.time()
        self._events.append(event)
        if self._event_callback is not None:
            self._event_callback(event)

    def _pre_run_risk_check_internal(self, wallet_id: str, lane_id: str) -> Tuple[bool, Optional[str]]:
        """Run pre-run risk gates: use injected callable or built-in STOP + global pause."""
        if self._pre_run_risk_check is not None:
            return self._pre_run_risk_check(wallet_id, lane_id)
        if self._state_path is not None and _stop_file_present(self._state_path):
            return False, "stop_file"
        if self._get_run_state is not None and _global_pause_active(self._get_run_state):
            return False, "global_rpc_pause"
        return True, None

    def _run_one_step_stub(self, wallet_id: str, lane_id: str) -> str:
        """
        Run one strategy step or no-op stub (no real execution).
        Returns outcome: "stubbed" | "step_skipped" | "no_strategy" | "guard_blocked".
        """
        # Strategy resolution already done by caller; we only need to return stubbed outcome.
        return "stubbed"

    def run_cycle(
        self,
        now: Optional[float] = None,
        cycle_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run one engine cycle: get eligible lanes, emit cycle_start, process each
        (wallet_id, lane_id) with re-check wallet state, strategy resolve, pre-run
        risk gates, one step or stub, post-run checks, lane_run and guard_triggered
        events, then cycle_end.

        Returns summary dict (cycle_index, duration_ms, counts) for callers.
        """
        if now is None:
            now = time.time()
        cycle_index = getattr(self, "_cycle_count", 0)
        self._cycle_count = cycle_index + 1
        if cycle_id is None:
            cycle_id = str(cycle_index)

        eligible = self._lane_manager.get_eligible_lanes(
            self._wallet_state_source,
            now=now,
        )
        # Deterministic order for reproducibility and monitoring.
        eligible = sorted(eligible, key=lambda x: (x[0], x[1]))

        self._emit({
            "event_type": "cycle_start",
            "cycle_id": cycle_id,
            "cycle_index": cycle_index,
            "eligible_count": len(eligible),
        })

        lane_runs_started = 0
        lane_runs_completed = 0
        lane_skipped = 0
        guard_triggered_count = 0

        for wallet_id, lane_id in eligible:
            # Re-check wallet state (defense in depth).
            state = _resolve_wallet_state_for_engine(self._wallet_state_source, wallet_id)
            if state != ACTIVE:
                lane_skipped += 1
                self._emit({
                    "event_type": "lane_skipped",
                    "wallet_id": wallet_id,
                    "lane_id": lane_id,
                    "reason": "wallet_not_active",
                    "wallet_state": state,
                })
                continue

            strategy = _get_strategy(self._strategy_registry, lane_id)
            if strategy is None:
                lane_skipped += 1
                self._emit({
                    "event_type": "lane_skipped",
                    "wallet_id": wallet_id,
                    "lane_id": lane_id,
                    "reason": "no_strategy",
                })
                continue

            # Resolve wallet identity only when we need it (for this wallet_id only).
            if self._wallet_manager is not None:
                try:
                    self._wallet_manager.resolve_identity(wallet_id)
                except Exception:
                    lane_skipped += 1
                    self._emit({
                        "event_type": "lane_skipped",
                        "wallet_id": wallet_id,
                        "lane_id": lane_id,
                        "reason": "resolve_identity_failed",
                    })
                    continue

            # Pre-run risk gates.
            allowed, guard_type = self._pre_run_risk_check_internal(wallet_id, lane_id)
            if not allowed:
                guard_triggered_count += 1
                lane_skipped += 1
                self._emit({
                    "event_type": "guard_triggered",
                    "guard_type": guard_type or "unknown",
                    "wallet_id": wallet_id,
                    "lane_id": lane_id,
                })
                continue

            lane_runs_started += 1
            if self._run_one_lane is not None:
                outcome = self._run_one_lane(wallet_id, lane_id)
            else:
                outcome = self._run_one_step_stub(wallet_id, lane_id)
            lane_runs_completed += 1

            # Post-run checks: stub only (no state update in T14).
            # Emit lane_run.
            self._emit({
                "event_type": "lane_run",
                "wallet_id": wallet_id,
                "lane_id": lane_id,
                "outcome": outcome,
            })

        duration_ms = (time.time() - now) * 1000.0
        self._emit({
            "event_type": "cycle_end",
            "cycle_id": cycle_id,
            "cycle_index": cycle_index,
            "duration_ms": duration_ms,
            "lane_runs_started": lane_runs_started,
            "lane_runs_completed": lane_runs_completed,
            "lane_skipped": lane_skipped,
            "guard_triggered_count": guard_triggered_count,
        })

        return {
            "cycle_id": cycle_id,
            "cycle_index": cycle_index,
            "duration_ms": duration_ms,
            "eligible_count": len(eligible),
            "lane_runs_started": lane_runs_started,
            "lane_runs_completed": lane_runs_completed,
            "lane_skipped": lane_skipped,
            "guard_triggered_count": guard_triggered_count,
        }

    @property
    def events(self) -> List[dict]:
        """Accumulated events from run_cycle (if no external list was passed)."""
        return self._events


def build_lane_manager_from_config(assignment_config: List[dict]) -> LaneManager:
    """Build a LaneManager from a list of assignment dicts (e.g. from JSON)."""
    lm = LaneManager()
    lm.load_from_dict(assignment_config)
    return lm
