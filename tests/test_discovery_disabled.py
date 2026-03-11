"""
Discovery disabled = complete no-op.

When discovery_enabled=False (default), DiscoveryPipeline.run() must:
- return an empty list
- not mutate state in any way (no records added, stats unchanged)
- not call any source adapters
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mint_ladder_bot.config import Config
from mint_ladder_bot.models import DiscoveryStats, RuntimeState, SolBalance
from mint_ladder_bot.sniper_engine.discovery.pipeline import DiscoveryPipeline


def _make_state() -> RuntimeState:
    return RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        wallet="WALLET_OK",
        sol=SolBalance(lamports=0, sol=0.0),
        mints={},
    )


def test_pipeline_disabled_returns_empty() -> None:
    cfg = Config()
    cfg.discovery_enabled = False
    state = _make_state()
    pipeline = DiscoveryPipeline(cfg)
    result = pipeline.run(state=state)
    assert result == []


def test_pipeline_disabled_does_not_mutate_state() -> None:
    cfg = Config()
    cfg.discovery_enabled = False
    state = _make_state()
    before_recent = list(state.discovery_recent_candidates)
    before_rejected = list(state.discovery_rejected_candidates)
    before_stats = state.discovery_stats.model_dump()

    pipeline = DiscoveryPipeline(cfg)
    pipeline.run(state=state)

    assert state.discovery_recent_candidates == before_recent
    assert state.discovery_rejected_candidates == before_rejected
    assert state.discovery_stats.model_dump() == before_stats


def test_sniper_service_process_candidate_queue_noop_when_disabled() -> None:
    """process_candidate_queue must be no-op when sniper disabled (default)."""
    from mint_ladder_bot.sniper_engine.service import SniperService

    cfg = Config()
    # sniper_enabled=False by default
    state = _make_state()
    svc = SniperService(config=cfg, state=state)
    svc.process_candidate_queue()  # must not raise

    assert state.discovery_recent_candidates == []
    assert state.discovery_rejected_candidates == []
    assert state.discovery_stats.total_discovered == 0


def test_sniper_service_process_noop_when_sniper_enabled_but_discovery_disabled() -> None:
    """Sniper enabled but discovery_enabled=False → still no-op."""
    from mint_ladder_bot.sniper_engine.service import SniperService

    cfg = Config()
    cfg.sniper_enabled = True
    cfg.sniper_mode = "paper"
    cfg.discovery_enabled = False  # explicit false
    state = _make_state()
    svc = SniperService(config=cfg, state=state)
    svc.process_candidate_queue()

    assert state.discovery_stats.total_discovered == 0
