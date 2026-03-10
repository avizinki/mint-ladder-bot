"""
Tests for lot entry price reconstruction invariant.

Invariant: lot.entry_price_sol_per_token must come only from transaction-derived quote value.
Market/bootstrap price must never be written into lot.entry_price_sol_per_token.

- SOL -> token: entry = sol_spent / tokens_received
- token -> token with WSOL in: entry = WSOL-equivalent / tokens_received
- token -> token with source-lot cost: entry = source FIFO cost / tokens_received
- token -> token with no source-lot cost and no WSOL: entry remains null
- _mint_market_bootstrap_entry must never be used to set lot entry
- Backfill must not mutate null entry into market price
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mint_ladder_bot.models import LotInfo, RuntimeMintState, RuntimeState
from mint_ladder_bot.tx_lot_engine import (
    WSOL_MINT,
    _mint_market_bootstrap_entry,
    _parse_buy_events_from_tx,
    run_tx_first_lot_engine,
)


def _tx_sol_to_token(wallet: str, mint: str, sol_spent_lamports: int, token_received_raw: int, fee: int = 5000):
    """Minimal tx dict: SOL decrease, token increase for one mint."""
    account_keys = [wallet, "other11111111111111111111111111111111111"]
    wallet_idx = 0
    pre_balances = [1_000_000_000, 0]
    post_balances = [pre_balances[0] - sol_spent_lamports - fee, 0]
    return {
        "transaction": {"message": {"accountKeys": account_keys}},
        "meta": {
            "fee": fee,
            "preBalances": pre_balances,
            "postBalances": post_balances,
            "preTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "0"}}],
            "postTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(token_received_raw)}}],
        },
        "blockTime": int(datetime.now(tz=timezone.utc).timestamp()),
        "slot": 100,
    }


def _tx_token_to_token(
    wallet: str,
    input_mint: str,
    input_delta_raw: int,
    output_mint: str,
    output_delta_raw: int,
    sol_delta_lamports: int = 0,
    fee: int = 5000,
):
    """Minimal tx dict: one token out, one token in (token->token)."""
    account_keys = [wallet, "other11111111111111111111111111111111111"]
    pre_balances = [500_000_000, 0]
    post_balances = [pre_balances[0] + sol_delta_lamports - fee, 0]
    pre_token = [
        {"owner": wallet, "mint": input_mint, "uiTokenAmount": {"amount": str(abs(input_delta_raw))}},
        {"owner": wallet, "mint": output_mint, "uiTokenAmount": {"amount": "0"}},
    ]
    post_token = [
        {"owner": wallet, "mint": input_mint, "uiTokenAmount": {"amount": "0"}},
        {"owner": wallet, "mint": output_mint, "uiTokenAmount": {"amount": str(output_delta_raw)}},
    ]
    return {
        "transaction": {"message": {"accountKeys": account_keys}},
        "meta": {
            "fee": fee,
            "preBalances": pre_balances,
            "postBalances": post_balances,
            "preTokenBalances": pre_token,
            "postTokenBalances": post_token,
        },
        "blockTime": int(datetime.now(tz=timezone.utc).timestamp()),
        "slot": 101,
    }


def test_sol_to_token_entry_from_sol_spent():
    """SOL -> token buy => entry set from sol_spent / tokens_received."""
    wallet = "wallet1111111111111111111111111111111111111"
    mint = "tokenmint11111111111111111111111111111111111"
    sol_spent = 100_000_000  # 0.1 SOL
    token_raw = 1_000_000  # 1e6 raw = 1 token if decimals=6
    decimals = 6
    tx = _tx_sol_to_token(wallet, mint, sol_spent, token_raw)
    events = _parse_buy_events_from_tx(
        tx, wallet, "sig_sol_token", {mint}, {mint: decimals}
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.entry_price_sol_per_token is not None
    expected = (sol_spent / 1e9) / (token_raw / (10 ** decimals))
    assert ev.entry_price_sol_per_token == pytest.approx(expected)
    assert ev.confidence == "known"
    assert ev.swap_type == "sol_to_token"


def test_token_to_token_wsol_entry_from_wsol_equiv():
    """token -> token with WSOL in => entry set from WSOL-equivalent quote."""
    wallet = "wallet1111111111111111111111111111111111111"
    dest_mint = "destmint111111111111111111111111111111111"
    wsol_spent_raw = 50_000_000  # 0.05 SOL in raw (6 decimals for WSOL)
    token_received_raw = 2_000_000  # 2 tokens
    decimals = 6
    tx = _tx_token_to_token(wallet, WSOL_MINT, -wsol_spent_raw, dest_mint, token_received_raw)
    events = _parse_buy_events_from_tx(
        tx, wallet, "sig_wsol_token", {dest_mint}, {dest_mint: decimals}
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.entry_price_sol_per_token is not None
    sol_equiv = wsol_spent_raw / 1e9
    token_human = token_received_raw / (10 ** decimals)
    expected = sol_equiv / token_human
    assert ev.entry_price_sol_per_token == pytest.approx(expected)
    assert ev.confidence == "inferred"
    assert ev.input_asset_mint == WSOL_MINT


def test_token_to_token_non_sol_entry_remains_null():
    """token -> token with no WSOL and no source-lot cost => entry remains null."""
    wallet = "wallet1111111111111111111111111111111111111"
    input_mint = "bigtrout111111111111111111111111111111111"
    dest_mint = "hachimint11111111111111111111111111111111"
    tx = _tx_token_to_token(wallet, input_mint, -1_000_000, dest_mint, 500_000)
    events = _parse_buy_events_from_tx(
        tx, wallet, "sig_tt", {dest_mint}, {dest_mint: 6, input_mint: 6}
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.entry_price_sol_per_token is None
    assert ev.confidence == "unknown"
    assert ev.input_asset_mint == input_mint


def test_mint_market_bootstrap_entry_returns_value_but_must_not_be_used_for_lot():
    """_mint_market_bootstrap_entry may return a value; callers must NOT write it to lot.entry."""
    from datetime import datetime, timezone
    state = RuntimeState(
        started_at=datetime.now(timezone.utc),
        status_file="status.json",
        mints={},
    )
    ms = RuntimeMintState(
        entry_price_sol_per_token=5.53e-11,
        original_entry_price_sol_per_token=5.53e-11,
        working_entry_price_sol_per_token=5.53e-11,
        trading_bag_raw="0",
        moonbag_raw="0",
    )
    state.mints["destmint11111111111111111111111111111111"] = ms
    res = _mint_market_bootstrap_entry(state, "destmint11111111111111111111111111111111")
    assert res is not None
    price, method = res
    assert price == 5.53e-11
    assert method == "mint_market_bootstrap"
    # Invariant: this value must never be assigned to lot.entry_price_sol_per_token.
    # Tests that use run_tx_first_lot_engine verify no lot gets this value for token->token.
    assert "Must NOT be used for lot entry" in (_mint_market_bootstrap_entry.__doc__ or "")


def test_token_to_token_no_backfill_of_null_entry_to_market():
    """Run tx-first engine for token->token (no source lots): created lot must have entry null, not market."""
    from datetime import datetime, timezone
    wallet = "wallet1111111111111111111111111111111111111"
    input_mint = "bigtrout111111111111111111111111111111111"
    dest_mint = "hachimint11111111111111111111111111111111"
    state = RuntimeState(
        started_at=datetime.now(timezone.utc),
        status_file="status.json",
        mints={},
    )
    ms = RuntimeMintState(
        entry_price_sol_per_token=5.53e-11,  # mint-level market bootstrap
        original_entry_price_sol_per_token=5.53e-11,
        working_entry_price_sol_per_token=5.53e-11,
        trading_bag_raw="0",
        moonbag_raw="0",
    )
    state.mints[dest_mint] = ms
    decimals_by_mint = {dest_mint: 6, input_mint: 6}

    tx = _tx_token_to_token(wallet, input_mint, -1_000_000, dest_mint, 500_000)

    class FakeRpc:
        def get_transaction(self, signature: str):
            return tx
        def get_signatures_for_address(self, addr: str, limit: int, before: str = None):
            return [{"signature": "sig_tt_no_backfill"}]

    rpc = FakeRpc()
    n = run_tx_first_lot_engine(
        state, rpc, wallet, decimals_by_mint, journal_path=None, max_signatures=1
    )
    assert n == 1
    lots = getattr(state.mints[dest_mint], "lots", None) or []
    assert len(lots) == 1
    lot = lots[0]
    # Lot must NOT have been filled with mint-level market bootstrap (5.53e-11).
    assert getattr(lot, "entry_price_sol_per_token", None) is None
    assert getattr(lot, "entry_confidence", None) == "unknown" or getattr(lot, "entry_confidence", None) == "inferred"


def test_token_to_token_with_source_lot_cost_gets_entry_from_fifo():
    """token -> token with source mint having lots with valid entry => entry from source FIFO cost."""
    wallet = "wallet1111111111111111111111111111111111111"
    source_mint = "sourcemint1111111111111111111111111111111"
    dest_mint = "destmint11111111111111111111111111111111"
    state = RuntimeState(
        started_at=datetime.now(timezone.utc),
        status_file="status.json",
        mints={},
    )
    source_lot = LotInfo.create(
        mint=source_mint,
        token_amount_raw=2_000_000,
        entry_price=1e-9,
        confidence="known",
        source="tx_exact",
        entry_confidence="exact",
        tx_signature="sig_prev",
        detected_at=datetime.now(timezone.utc),
    )
    source_lot.remaining_amount = str(2_000_000)
    source_ms = RuntimeMintState(lots=[source_lot], entry_price_sol_per_token=1e-9, trading_bag_raw="2000000", moonbag_raw="0")
    state.mints[source_mint] = source_ms
    dest_ms = RuntimeMintState(entry_price_sol_per_token=0.0, trading_bag_raw="0", moonbag_raw="0")
    state.mints[dest_mint] = dest_ms

    tx = _tx_token_to_token(wallet, source_mint, -1_000_000, dest_mint, 500_000)
    events = _parse_buy_events_from_tx(
        tx, wallet, "sig_tt_fifo", {dest_mint}, {dest_mint: 6, source_mint: 6}
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.entry_price_sol_per_token is None  # parser leaves null for non-WSOL

    # Enrich: run the same enrichment logic as run_tx_first_lot_engine (source_lot_cost path)
    from mint_ladder_bot.tx_lot_engine import (
        _source_cost_basis_sol,
        _validate_entry_price,
    )
    dec = 6
    token_human = ev.token_amount_raw / (10 ** dec)
    res = _source_cost_basis_sol(state, ev.input_asset_mint, ev.input_amount_raw, {source_mint: 6, dest_mint: 6})
    assert res is not None
    cost_sol, method = res
    ev.entry_price_sol_per_token = cost_sol / token_human
    assert _validate_entry_price(ev.entry_price_sol_per_token)
    assert ev.entry_price_sol_per_token == pytest.approx(2e-9)  # 1e-9 * (1M/1M) / 0.5 = 2e-9 per dest token


def test_token_to_token_source_with_unknown_snapshot_before_tx_lot_keeps_entry_unknown():
    """
    When the source mint has an older snapshot lot with unknown entry ahead of a tx_exact lot,
    token->token enrichment must NOT infer cost basis from the tx_exact lot (FIFO alignment).
    Entry must remain null so we do not ascribe cost to tokens that may have come from the
    unknown snapshot lot.
    """
    wallet = "wallet1111111111111111111111111111111111111"
    source_mint = "sourcemint_snapshot11111111111111111111"
    dest_mint = "destmint_snapshot11111111111111111111111"

    state = RuntimeState(
        started_at=datetime.now(timezone.utc),
        status_file="status.json",
        mints={},
    )

    # Older snapshot lot with unknown entry (bootstrap / reconciliation), first in FIFO.
    snapshot_lot = LotInfo.create(
        mint=source_mint,
        token_amount_raw=1_000_000,
        entry_price=None,
        confidence="unknown",
        source="bootstrap_snapshot",
        entry_confidence="snapshot",
    )
    snapshot_lot.remaining_amount = snapshot_lot.token_amount

    # Newer tx_exact lot with valid entry, second in FIFO.
    tx_lot = LotInfo.create(
        mint=source_mint,
        token_amount_raw=1_000_000,
        entry_price=1e-9,
        confidence="known",
        source="tx_exact",
        entry_confidence="exact",
        tx_signature="sig_prev",
        detected_at=datetime.now(timezone.utc),
    )
    tx_lot.remaining_amount = tx_lot.token_amount

    source_ms = RuntimeMintState(
        lots=[snapshot_lot, tx_lot],
        entry_price_sol_per_token=1e-9,
        trading_bag_raw=str(int(snapshot_lot.token_amount) + int(tx_lot.token_amount)),
        moonbag_raw="0",
    )
    state.mints[source_mint] = source_ms
    dest_ms = RuntimeMintState(entry_price_sol_per_token=0.0, trading_bag_raw="0", moonbag_raw="0")
    state.mints[dest_mint] = dest_ms

    # Token->token swap that must consume from the snapshot lot in FIFO order.
    tx = _tx_token_to_token(wallet, source_mint, -1_000_000, dest_mint, 500_000)
    events = _parse_buy_events_from_tx(
        tx, wallet, "sig_tt_snapshot_first", {dest_mint}, {dest_mint: 6, source_mint: 6}
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.entry_price_sol_per_token is None  # parser leaves null for non-WSOL

    from mint_ladder_bot.tx_lot_engine import _source_cost_basis_sol

    res = _source_cost_basis_sol(state, ev.input_asset_mint, ev.input_amount_raw, {source_mint: 6, dest_mint: 6})
    # Because the first FIFO lot has unknown entry, enrichment must refuse to infer cost and keep entry null.
    assert res is None
