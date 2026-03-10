from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mint_ladder_bot.config import Config
from mint_ladder_bot.models import LotInfo, RuntimeMintState, RuntimeState, SolBalance
from mint_ladder_bot.sniper_engine.service import SniperService


def _make_state() -> RuntimeState:
    return RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        wallet="WALLET_OK",
        sol=SolBalance(lamports=0, sol=0.0),
        mints={},
    )


def test_sniper_service_modes_disabled_by_default() -> None:
    cfg = Config()
    state = _make_state()
    svc = SniperService(config=cfg, state=state)
    assert svc.mode() in ("disabled", "paper", "live")
    assert not svc.is_enabled()
    assert not svc.is_live_mode()
    assert not svc.is_paper_mode()


def test_enqueue_rejected_when_disabled() -> None:
    cfg = Config()
    state = _make_state()
    svc = SniperService(config=cfg, state=state)
    accepted, reason, size = svc.enqueue_manual_seed("SomeMint", note=None)
    assert not accepted
    assert reason == "disabled"
    assert size == 0


def test_enqueue_accepts_valid_when_enabled(monkeypatch) -> None:
    cfg = Config()
    # Force-enable sniper + live mode for the service.
    cfg.sniper_enabled = True
    cfg.sniper_mode = "live"
    cfg.sniper_max_manual_queue_size = 10
    state = _make_state()
    svc = SniperService(config=cfg, state=state)

    accepted, reason, size = svc.enqueue_manual_seed("MintA", note="test")
    assert accepted
    assert reason is None
    assert size == 1
    assert state.sniper_manual_seed_queue[0].mint == "MintA"


def test_enqueue_rejects_duplicate_in_queue() -> None:
    cfg = Config()
    cfg.sniper_enabled = True
    cfg.sniper_mode = "live"
    cfg.sniper_max_manual_queue_size = 10
    state = _make_state()
    svc = SniperService(config=cfg, state=state)

    ok, _, _ = svc.enqueue_manual_seed("MintA", note=None)
    assert ok
    accepted, reason, size = svc.enqueue_manual_seed("MintA", note=None)
    assert not accepted
    assert reason == "duplicate_in_queue"
    assert size == 1


def test_enqueue_rejects_queue_full() -> None:
    cfg = Config()
    cfg.sniper_enabled = True
    cfg.sniper_mode = "live"
    cfg.sniper_max_manual_queue_size = 1
    state = _make_state()
    svc = SniperService(config=cfg, state=state)

    ok, _, _ = svc.enqueue_manual_seed("MintA", note=None)
    assert ok
    accepted, reason, size = svc.enqueue_manual_seed("MintB", note=None)
    assert not accepted
    assert reason == "queue_full"
    assert size == 1


def test_enqueue_rejects_when_open_lot_exists() -> None:
    cfg = Config()
    cfg.sniper_enabled = True
    cfg.sniper_mode = "live"
    cfg.sniper_max_manual_queue_size = 10
    state = _make_state()
    # Create an open lot for MintA
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw="1000",
        moonbag_raw="0",
        lots=[LotInfo(mint="MintA", token_amount="1000", remaining_amount="1000")],
    )
    state.mints["MintA"] = ms
    svc = SniperService(config=cfg, state=state)

    accepted, reason, size = svc.enqueue_manual_seed("MintA", note=None)
    assert not accepted
    assert reason == "open_lot_exists"
    assert size == 0

