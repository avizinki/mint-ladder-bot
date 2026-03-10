from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from mint_ladder_bot.main import _validate_startup_for_run
from mint_ladder_bot.models import StatusFile, SolBalance, RpcInfo, RuntimeState
from mint_ladder_bot.state import save_state_atomic


def _mk_status(tmp_path: Path, wallet: str = "WALLET_OK") -> Path:
    status_path = tmp_path / "status.json"
    status = StatusFile(
        version=1,
        created_at=datetime.now(tz=timezone.utc),
        wallet=wallet,
        rpc=RpcInfo(endpoint="http://localhost", latency_ms=None),
        sol=SolBalance(lamports=0, sol=0.0),
        mints=[],
    )
    status_path.write_text(status.model_dump_json(indent=2), encoding="utf-8")
    return status_path


def _mk_state(tmp_path: Path, status_path: Path) -> Path:
    state_path = tmp_path / "state.json"
    state = RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file=str(status_path),
        wallet="WALLET_OK",
        sol=SolBalance(lamports=0, sol=0.0),
        mints={},
    )
    save_state_atomic(state_path, state)
    return state_path


def test_startup_validation_missing_status_fails(tmp_path, monkeypatch):
    from mint_ladder_bot.config import Config

    status_path = tmp_path / "status.json"  # does not exist
    state_path = tmp_path / "state.json"
    cfg = Config()

    import click

    with pytest.raises(click.exceptions.Exit):
        _validate_startup_for_run(status=status_path, state=state_path, config=cfg)


def test_startup_validation_corrupted_state_fails(tmp_path):
    from mint_ladder_bot.config import Config

    status_path = _mk_status(tmp_path)
    state_path = tmp_path / "state.json"
    state_path.write_text("{not-json", encoding="utf-8")
    cfg = Config()

    import click

    with pytest.raises(click.exceptions.Exit):
        _validate_startup_for_run(status=status_path, state=state_path, config=cfg)


def test_startup_validation_wallet_mismatch_fails(tmp_path, monkeypatch):
    from mint_ladder_bot.config import Config

    # Status has one wallet, but we will fake a different derived wallet.
    status_path = _mk_status(tmp_path, wallet="STATUS_WALLET")
    state_path = _mk_state(tmp_path, status_path)

    class _FakeKeypair:
        def pubkey(self):
            class _Pk:
                def __str__(self):
                    return "DERIVED_WALLET"

            return _Pk()

    # Monkeypatch wallet_manager.resolve_keypair to return a keypair with a different pubkey.
    import mint_ladder_bot.main as main_mod
    from mint_ladder_bot import wallet_manager

    monkeypatch.setattr(wallet_manager, "resolve_keypair", lambda *_args, **_kwargs: _FakeKeypair())

    cfg = Config()
    import click
    with pytest.raises(click.exceptions.Exit):
        _validate_startup_for_run(status=status_path, state=state_path, config=cfg)


def test_startup_validation_succeeds_on_valid_inputs(tmp_path, monkeypatch):
    from mint_ladder_bot.config import Config

    status_path = _mk_status(tmp_path, wallet="DERIVED_WALLET")
    state_path = _mk_state(tmp_path, status_path)

    class _FakeKeypair:
        def pubkey(self):
            class _Pk:
                def __str__(self):
                    return "DERIVED_WALLET"

            return _Pk()

    from mint_ladder_bot import wallet_manager

    monkeypatch.setattr(wallet_manager, "resolve_keypair", lambda *_args, **_kwargs: _FakeKeypair())

    cfg = Config()
    # Should not raise SystemExit when inputs are consistent and state is valid.
    _validate_startup_for_run(status=status_path, state=state_path, config=cfg)

