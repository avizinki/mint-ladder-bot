from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from mint_ladder_bot.models import LotInfo, RuntimeMintState, RuntimeState
from mint_ladder_bot.tx_lot_engine import BuyEvent, run_tx_first_lot_engine


class _FakeRpc:
    def __init__(self, signatures: List[str], txs: Dict[str, Dict[str, Any]]):
        self._sigs = signatures
        self._txs = txs

    def get_signatures_for_address(self, _addr: str, limit: int, before: str | None = None):
        return [{"signature": s} for s in self._sigs[:limit]]

    def get_transaction(self, signature: str) -> Dict[str, Any]:
        return self._txs.get(signature, {})


def test_token_to_token_disposal_idempotent(monkeypatch):
    """
    Token-to-token source disposals must be idempotent across replays:
    a given (signature, source_mint) may only debit source lots once.
    """

    source_mint = "SRC"
    dest_mint = "DST"
    sig = "SIG_TT"

    # Source mint has one active tx_exact lot.
    src_lot = LotInfo.create(
        mint=source_mint,
        token_amount_raw=1_000_000,
        entry_price=1e-6,
        confidence="known",
        source="tx_exact",
    )
    src_ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw=src_lot.token_amount,
        moonbag_raw="0",
        lots=[src_lot],
    )
    # Destination mint exists but has no lots yet.
    dst_ms = RuntimeMintState(
        entry_price_sol_per_token=0.0,
        trading_bag_raw="0",
        moonbag_raw="0",
        lots=[],
    )
    state = RuntimeState(
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        mints={source_mint: src_ms, dest_mint: dst_ms},
    )

    rpc = _FakeRpc(signatures=[sig], txs={sig: {"meta": {}, "slot": 1}})

    # Monkeypatch parser to emit a single token->token BuyEvent.
    def _fake_parse_buy_events_from_tx(tx: dict, wallet: str, signature: str, mints_tracked, decimals_by_mint):
        return [
            BuyEvent(
                signature=signature,
                mint=dest_mint,
                token_amount_raw=500_000,
                sol_spent_lamports=0,
                entry_price_sol_per_token=None,
                block_time=datetime.now(tz=timezone.utc),
                swap_type="token_to_token",
                input_asset_mint=source_mint,
                input_amount_raw=300_000,
                source_sold_raw=300_000,
            )
        ]

    import mint_ladder_bot.tx_lot_engine as txle

    monkeypatch.setattr(txle, "_parse_buy_events_from_tx", _fake_parse_buy_events_from_tx)

    # First run: should create a dest lot and debit source once.
    lots_created_1 = run_tx_first_lot_engine(
        state=state,
        rpc=rpc,
        wallet_pubkey="WALLET",
        decimals_by_mint={source_mint: 6, dest_mint: 6},
        journal_path=None,
        max_signatures=10,
    )
    assert lots_created_1 == 1
    # Source remaining decreased by source_sold_raw.
    assert int(src_ms.lots[0].remaining_amount) == 1_000_000 - 300_000
    assert dest_mint in state.mints and len(state.mints[dest_mint].lots) == 1
    processed = getattr(state, "processed_token_to_token_disposals", [])
    assert f"{sig}|{source_mint}" in processed

    # Capture state snapshot.
    src_remaining_after_first = int(src_ms.lots[0].remaining_amount)
    disposals_len_after_first = len(processed)

    # Second run with same history must not change source or disposals.
    lots_created_2 = run_tx_first_lot_engine(
        state=state,
        rpc=rpc,
        wallet_pubkey="WALLET",
        decimals_by_mint={source_mint: 6, dest_mint: 6},
        journal_path=None,
        max_signatures=10,
    )
    assert lots_created_2 == 0
    assert int(src_ms.lots[0].remaining_amount) == src_remaining_after_first
    processed_after = getattr(state, "processed_token_to_token_disposals", [])
    assert len(processed_after) == disposals_len_after_first

