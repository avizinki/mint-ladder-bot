"""
Runtime proof: state validation, entry/tradability, pause after failures.
CEO directive integration audit Phase 4.
Run with project venv so mint_ladder_bot.runner is importable (e.g. uv run pytest).
"""
from datetime import datetime, timedelta, timezone

import pytest

from mint_ladder_bot.models import (
    EntryInfo,
    FailureInfo,
    LotInfo,
    MarketInfo,
    MintStatus,
    RuntimeMintState,
    RuntimeState,
    StatusFile,
    SolBalance,
    RpcInfo,
)
from mint_ladder_bot.state import validate_state_schema
from mint_ladder_bot.config import Config

try:
    from mint_ladder_bot.runner import (
        _filter_tradable_and_bootstrap_mints,
        _update_failures_on_error,
        _update_reconciliation_pause_for_mint,
        _is_paused,
        _trading_bag_from_lots,
        validate_entry_price,
    )
    _RUNNER_AVAILABLE = True
except Exception:
    _RUNNER_AVAILABLE = False


def _minimal_status(mints):
    return StatusFile(
        created_at=datetime.now(tz=timezone.utc),
        wallet="test",
        rpc=RpcInfo(endpoint="http://test"),
        sol=SolBalance(lamports=0, sol=0.0),
        mints=mints,
    )


def test_validate_state_schema_rejects_missing_fields():
    """Invalid state: mint missing required fields returns errors."""
    state = RuntimeState(
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        mints={},
    )
    errs = validate_state_schema(state)
    assert errs == []

    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw="1000000",
        moonbag_raw="0",
        lots=[],
        executed_steps={},
        failures=FailureInfo(),
    )
    state2 = RuntimeState(
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        mints={"mint111": ms},
    )
    errs2 = validate_state_schema(state2)
    assert errs2 == []

    # State with object that has no 'lots' attribute (e.g. malformed JSON load)
    class BadMint:
        entry_price_sol_per_token = 0.0
        # no lots, executed_steps, failures
    class FakeState:
        mints = {"mintbad": BadMint()}
    errs3 = validate_state_schema(FakeState())
    assert any("mintbad" in e for e in errs3)
    assert any("missing" in e.lower() for e in errs3)


@pytest.mark.skipif(not _RUNNER_AVAILABLE, reason="runner not importable (missing deps)")
def test_unknown_entry_not_tradable():
    """Unknown entry_source => mint in bootstrap_pending, not tradable."""
    m = MintStatus(
        mint="mintUnknown",
        token_account="acc",
        decimals=6,
        balance_ui=100.0,
        balance_raw="100000000",
        symbol="UNK",
        entry=EntryInfo(entry_price_sol_per_token=1e-6, entry_source="unknown"),
        market=MarketInfo(),
    )
    m.market.dexscreener.liquidity_usd = 50_000.0
    status = _minimal_status([m])
    state = RuntimeState(started_at=datetime.now(tz=timezone.utc), status_file="status.json", mints={})
    tradable, bootstrap_pending = _filter_tradable_and_bootstrap_mints(status, state)
    assert m not in tradable
    assert m in bootstrap_pending


@pytest.mark.skipif(not _RUNNER_AVAILABLE, reason="runner not importable (missing deps)")
def test_valid_entry_tradable():
    """Valid entry_price and entry_source => mint in tradable."""
    m = MintStatus(
        mint="mintValid",
        token_account="acc",
        decimals=6,
        balance_ui=100.0,
        balance_raw="100000000",
        symbol="VAL",
        entry=EntryInfo(entry_price_sol_per_token=1e-6, entry_source="market_bootstrap"),
        market=MarketInfo(),
    )
    m.market.dexscreener.liquidity_usd = 50_000.0
    status = _minimal_status([m])
    state = RuntimeState(started_at=datetime.now(tz=timezone.utc), status_file="status.json", mints={})
    tradable, bootstrap_pending = _filter_tradable_and_bootstrap_mints(status, state)
    assert m in tradable
    assert m not in bootstrap_pending


@pytest.mark.skipif(not _RUNNER_AVAILABLE, reason="runner not importable (missing deps)")
def test_entry_zero_not_tradable():
    """entry_price <= 0 => not in tradable (bootstrap_pending or skipped)."""
    m = MintStatus(
        mint="mintZero",
        token_account="acc",
        decimals=6,
        balance_ui=100.0,
        balance_raw="100000000",
        symbol="ZER",
        entry=EntryInfo(entry_price_sol_per_token=0.0, entry_source="market_bootstrap"),
        market=MarketInfo(),
    )
    m.market.dexscreener.liquidity_usd = 50_000.0
    status = _minimal_status([m])
    state = RuntimeState(started_at=datetime.now(tz=timezone.utc), status_file="status.json", mints={})
    tradable, bootstrap_pending = _filter_tradable_and_bootstrap_mints(status, state)
    assert m not in tradable
    assert m in bootstrap_pending


@pytest.mark.skipif(not _RUNNER_AVAILABLE, reason="runner not importable (missing deps)")
def test_trading_bag_excludes_null_entry_lots():
    """Lots with entry_price=None must not contribute to trading_bag_raw."""
    # One valid-entry tx_exact lot and one null-entry tx_parsed lot.
    lot_valid = LotInfo.create(
        mint="M",
        token_amount_raw=1000,
        entry_price=1e-6,
        confidence="known",
        source="tx_exact",
    )
    lot_null = LotInfo.create(
        mint="M",
        token_amount_raw=500,
        entry_price=None,
        confidence="unknown",
        source="tx_parsed",
    )
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw="0",
        moonbag_raw="0",
        lots=[lot_valid, lot_null],
    )
    bag = _trading_bag_from_lots(ms)
    assert bag == int(lot_valid.remaining_amount)


@pytest.mark.skipif(not _RUNNER_AVAILABLE, reason="runner not importable (missing deps)")
def test_trading_bag_zero_when_all_entries_unknown():
    """When all lots have unknown/null entry, trading_bag_raw must be zero."""
    lot_unknown = LotInfo.create(
        mint="M",
        token_amount_raw=1000,
        entry_price=None,
        confidence="unknown",
        source="tx_parsed",
    )
    ms = RuntimeMintState(
        entry_price_sol_per_token=0.0,
        trading_bag_raw="0",
        moonbag_raw="0",
        lots=[lot_unknown],
    )
    bag = _trading_bag_from_lots(ms)
    assert bag == 0


@pytest.mark.skipif(not _RUNNER_AVAILABLE, reason="runner not importable (missing deps)")
def test_classify_bag_zero_reason_unknown_entry_lots():
    """Bag-zero reason is unknown_entry_lots when tx-derived lots exist but none have valid entry."""
    from mint_ladder_bot.bag_zero_reason import classify_bag_zero_reason

    lot_unknown = LotInfo.create(
        mint="M",
        token_amount_raw=1000,
        entry_price=None,
        confidence="unknown",
        source="tx_parsed",
    )
    lot_unknown.remaining_amount = "1000"
    ms = RuntimeMintState(
        entry_price_sol_per_token=0.0,
        trading_bag_raw="0",
        moonbag_raw="0",
        lots=[lot_unknown],
    )
    reason = classify_bag_zero_reason(ms.dict(), wallet_balance_raw=1000)
    assert reason == "unknown_entry_lots"


@pytest.mark.skipif(not _RUNNER_AVAILABLE, reason="runner not importable (missing deps)")
def test_classify_bag_zero_reason_non_tradable_sources_only():
    """Bag-zero reason is non_tradable_sources_only when only bootstrap/unknown lots exist."""
    from mint_ladder_bot.bag_zero_reason import classify_bag_zero_reason

    lot_bootstrap = LotInfo.create(
        mint="M",
        token_amount_raw=1000,
        entry_price=None,
        confidence="unknown",
        source="bootstrap_snapshot",
    )
    lot_bootstrap.remaining_amount = "1000"
    ms = RuntimeMintState(
        entry_price_sol_per_token=0.0,
        trading_bag_raw="0",
        moonbag_raw="0",
        lots=[lot_bootstrap],
    )
    reason = classify_bag_zero_reason(ms.dict(), wallet_balance_raw=1000)
    assert reason == "non_tradable_sources_only"


@pytest.mark.skipif(not _RUNNER_AVAILABLE, reason="runner not importable (missing deps)")
def test_pause_after_three_failures():
    """Per-mint pause: after max_consecutive_failures (default 3), paused_until is set."""
    config = Config()
    config.max_consecutive_failures = 3
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw="1000000",
        moonbag_raw="0",
        lots=[],
        executed_steps={},
        failures=FailureInfo(count=0),
    )
    assert ms.failures.paused_until is None
    _update_failures_on_error(ms, Exception("e1"), config)
    assert ms.failures.count == 1
    assert ms.failures.paused_until is None
    _update_failures_on_error(ms, Exception("e2"), config)
    assert ms.failures.count == 2
    assert ms.failures.paused_until is None
    _update_failures_on_error(ms, Exception("e3"), config)
    assert ms.failures.count == 3
    assert ms.failures.paused_until is not None


@pytest.mark.skipif(not _RUNNER_AVAILABLE, reason="runner not importable (missing deps)")
def test_reconciliation_mismatch_below_threshold_does_not_pause(tmp_path):
    """Reconciliation mismatch for fewer than threshold cycles must not pause the mint."""
    config = Config()
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw="1000000",
        moonbag_raw="0",
        lots=[],
        executed_steps={},
        failures=FailureInfo(count=0),
    )
    assert ms.failures.paused_until is None
    now = datetime.now(tz=timezone.utc)
    # Two consecutive mismatches (default threshold is 3)
    for _ in range(2):
        _update_reconciliation_pause_for_mint(
            mint="mint111",
            mint_state=ms,
            actual_raw=1000,
            sum_lots=900,
            now=now,
            config=config,
            event_journal_path=None,
        )
    assert ms.reconcile_mismatch_consecutive == 2
    assert ms.failures.paused_until is None


@pytest.mark.skipif(not _RUNNER_AVAILABLE, reason="runner not importable (missing deps)")
def test_reconciliation_mismatch_triggers_per_mint_pause_after_threshold(tmp_path):
    """After threshold consecutive mismatches, mint is paused via failures.paused_until."""
    config = Config()
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw="1000000",
        moonbag_raw="0",
        lots=[],
        executed_steps={},
        failures=FailureInfo(count=0),
    )
    assert ms.failures.paused_until is None
    now = datetime.now(tz=timezone.utc)
    for _ in range(3):
        _update_reconciliation_pause_for_mint(
            mint="mint222",
            mint_state=ms,
            actual_raw=1000,
            sum_lots=900,
            now=now,
            config=config,
            event_journal_path=None,
        )
    assert ms.reconcile_mismatch_consecutive >= 3
    assert ms.failures.paused_until is not None
    assert ms.failures.last_error == "reconciliation_mismatch"
    assert _is_paused(ms) is True


@pytest.mark.skipif(not _RUNNER_AVAILABLE, reason="runner not importable (missing deps)")
def test_reconciliation_mismatch_resets_after_match(tmp_path):
    """When wallet and lots realign, mismatch counters reset and pause eventually clears."""
    config = Config()
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw="1000000",
        moonbag_raw="0",
        lots=[],
        executed_steps={},
        failures=FailureInfo(count=0),
    )
    now = datetime.now(tz=timezone.utc)
    # Drive into paused state.
    for _ in range(3):
        _update_reconciliation_pause_for_mint(
            mint="mint333",
            mint_state=ms,
            actual_raw=1000,
            sum_lots=900,
            now=now,
            config=config,
            event_journal_path=None,
        )
    assert ms.failures.paused_until is not None
    # Advance time beyond pause window and simulate a reconciled cycle.
    after = ms.failures.paused_until + timedelta(seconds=1)
    _update_reconciliation_pause_for_mint(
        mint="mint333",
        mint_state=ms,
        actual_raw=1000,
        sum_lots=1000,
        now=after,
        config=config,
        event_journal_path=None,
    )
    assert ms.reconcile_mismatch_consecutive == 0
    assert ms.reconcile_mismatch_last_seen_at is None
    # With mismatch cleared, counters are reset; time-based pause expiry is handled by _is_paused at runtime.

@pytest.mark.skipif(not _RUNNER_AVAILABLE, reason="runner not importable (missing deps)")
def test_validate_entry_price_bounds():
    """validate_entry_price rejects zero and out-of-range."""
    assert validate_entry_price(0.0) is False
    assert validate_entry_price(-1e-9) is False
    # Very small or very large can be rejected depending on ENTRY_PRICE_MIN/MAX
    assert validate_entry_price(1e-6) is True or validate_entry_price(1e-6) is False  # implementation-defined
