from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from mint_ladder_bot.models import RuntimeState


def _load_state(path: Path) -> RuntimeState:
    from mint_ladder_bot.state import load_state

    # status_file path is not used by projection; pass dummy.
    return load_state(path, status_file=path.parent / "status.json")


def _projection(state: RuntimeState) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for mint, ms in state.mints.items():
        lots = []
        for lot in getattr(ms, "lots", None) or []:
            lots.append(
                (
                    getattr(lot, "source", None),
                    getattr(lot, "token_amount", None),
                    getattr(lot, "remaining_amount", None),
                    getattr(lot, "entry_price_sol_per_token", None),
                )
            )
        out[mint] = {
            "trading_bag_raw": getattr(ms, "trading_bag_raw", None),
            "lots": sorted(lots),
        }
    return out


def test_rebuild_from_chain_idempotent_projection(monkeypatch, tmp_path):
    """
    Running rebuild_from_chain twice on the same wallet history should produce
    an economically identical state (same bags and lot amounts), modulo IDs/timestamps.
    """

    # Arrange fake wallet history: two simple buys of different mints.
    wallet = "WALLET_REBUILD"
    txs = [
        {"signature": "SIG_A", "slot": 1},
        {"signature": "SIG_B", "slot": 2},
    ]

    class _Ev:
        def __init__(self, signature: str, mint: str, token_delta: int, sol_delta: int):
            self.signature = signature
            self.mint = mint
            self.token_delta = token_delta
            self.sol_delta = sol_delta
            self.type = "buy"

    events = [
        _Ev("SIG_A", "MINT_A", token_delta=1_000_000, sol_delta=-1_000_000),
        _Ev("SIG_B", "MINT_B", token_delta=2_000_000, sol_delta=-2_000_000),
    ]

    import scripts.rebuild_from_chain as rbc

    def _fake_get_wallet_transactions(_wallet: str, limit: int = 100):
        assert _wallet == wallet
        return txs

    def _fake_map_helius_to_wallet_tx_events(_txs, _wallet):
        return events

    monkeypatch.setattr(rbc, "get_wallet_transactions", _fake_get_wallet_transactions)
    monkeypatch.setattr(rbc, "map_helius_to_wallet_tx_events", _fake_map_helius_to_wallet_tx_events)

    # Prepare temp project root with a minimal status.json.
    project_root = tmp_path
    state_path = project_root / "state.json"
    status_path = project_root / "status.json"
    status_path.write_text(json.dumps({"wallet": wallet}))

    # Helper to run main() with controlled argv.
    def _run_rebuild():
        argv_backup = sys.argv[:]
        try:
            sys.argv = [
                "rebuild_from_chain",
                "--state",
                str(state_path),
                "--status",
                str(status_path),
                "--wallet",
                wallet,
                "--project-root",
                str(project_root),
            ]
            rbc.main()
        finally:
            sys.argv = argv_backup

    # First rebuild.
    _run_rebuild()
    state1 = _load_state(state_path)
    proj1 = _projection(state1)

    # Second rebuild with same history.
    _run_rebuild()
    state2 = _load_state(state_path)
    proj2 = _projection(state2)

    assert proj1 == proj2

