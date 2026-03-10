from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from mint_ladder_bot.models import LotInfo, RuntimeMintState, RuntimeState, StepExecutionInfo
from mint_ladder_bot.tx_lot_engine import BuyEvent, run_tx_first_lot_engine
from mint_ladder_bot.runner import _ingest_external_sells


class _FakeRpc:
    """
    Minimal fake RpcClient for replay/idempotency tests.

    Returns a fixed set of signatures and tx dicts; does not hit network.
    """

    def __init__(self, signatures: List[str], txs: Dict[str, Dict[str, Any]]):
        self._sigs = signatures
        self._txs = txs

    def get_signatures_for_address(self, _addr: str, limit: int, before: str | None = None):
        out = [{"signature": s} for s in self._sigs[:limit]]
        return out

    def get_transaction(self, signature: str) -> Dict[str, Any]:
        return self._txs.get(signature, {})


def test_tx_first_lot_engine_idempotent_with_existing_lots(monkeypatch, tmp_path):
    """
    Given a state that already contains a tx-derived lot for (sig, mint),
    run_tx_first_lot_engine must not create a duplicate lot on replay.
    """

    # Prepare state with one mint and one existing tx-derived lot.
    mint = "MINT_TX_FIRST"
    sig = "SIG_TX_FIRST"
    lot = LotInfo.create(mint=mint, token_amount_raw=1_000_000, entry_price=1e-6, confidence="known", source="tx_exact", tx_signature=sig)
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw=lot.token_amount,
        moonbag_raw="0",
        lots=[lot],
    )
    state = RuntimeState(
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        mints={mint: ms},
    )

    # Fake RPC: returns the same signature and a dummy tx; parser is monkeypatched.
    rpc = _FakeRpc(signatures=[sig], txs={sig: {"meta": {}, "slot": 1}})

    # Monkeypatch parser to emit a BuyEvent for the same (sig, mint). Idempotency
    # should still come from existing_sig_mint inside run_tx_first_lot_engine.
    def _fake_parse_buy_events_from_tx(tx: dict, wallet: str, signature: str, mints_tracked, decimals_by_mint):
        return [
            BuyEvent(
                signature=signature,
                mint=mint,
                token_amount_raw=500_000,
                sol_spent_lamports=0,
                entry_price_sol_per_token=None,
                block_time=datetime.now(tz=timezone.utc),
            )
        ]

    import mint_ladder_bot.tx_lot_engine as txle

    monkeypatch.setattr(txle, "_parse_buy_events_from_tx", _fake_parse_buy_events_from_tx)

    # First run would have created the lot; we simulate that via initial state.
    lots_before = list(ms.lots)

    lots_created = run_tx_first_lot_engine(
        state=state,
        rpc=rpc,
        wallet_pubkey="WALLET",
        decimals_by_mint={mint: 6},
        journal_path=None,
        max_signatures=10,
    )
    # No new lots should be created because (sig, mint) already exists.
    assert lots_created == 0
    assert ms.lots == lots_before


def test_external_sell_ingestion_idempotent(monkeypatch, tmp_path):
    """
    _ingest_external_sells must be idempotent: for a given (mint, signature),
    the external sell should be ingested at most once (one executed_step, one debit).
    """

    mint = "MINT_EXT"
    initial_amount = 1_000_000
    sold_raw = 400_000

    lot = LotInfo.create(mint=mint, token_amount_raw=initial_amount, entry_price=1e-6, confidence="known", source="tx_exact")
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw=str(initial_amount),
        moonbag_raw="0",
        lots=[lot],
        executed_steps={},
    )
    state = RuntimeState(
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        mints={mint: ms},
    )

    sig = "SIG_EXT"

    class _FakeSellEvent:
        def __init__(self, mint: str, sold_raw: int, sol_in_lamports: int):
            self.mint = mint
            self.sold_raw = sold_raw
            self.sol_in_lamports = sol_in_lamports
            self.block_time = None

    # Fake RPC & parser for external sells.
    rpc = _FakeRpc(signatures=[sig], txs={sig: {"meta": {}, "slot": 1}})

    def _fake_parse_sell_events_from_tx(tx: dict, wallet: str, mints_tracked, signature: str):
        return [_FakeSellEvent(mint=mint, sold_raw=sold_raw, sol_in_lamports=1_000_000_000)]

    import mint_ladder_bot.runner as rmod

    monkeypatch.setattr(rmod, "parse_sell_events_from_tx", _fake_parse_sell_events_from_tx)

    # First ingestion: should create one external executed_step and debit lots once.
    ingested1 = _ingest_external_sells(state, rpc, wallet="WALLET", max_signatures=10, journal_path=None)
    assert ingested1 == 1
    assert len(ms.executed_steps) == 1
    step = next(iter(ms.executed_steps.values()))
    assert isinstance(step, StepExecutionInfo)
    assert int(step.sold_raw) == sold_raw
    # Lot should be debited once.
    assert int(ms.lots[0].remaining_amount) == initial_amount - sold_raw
    # Sell accounting invariant must hold.
    total_sold_steps = sum(int(getattr(s, "sold_raw", 0) or 0) for s in ms.executed_steps.values())
    sold_bot = int(ms.sold_bot_raw or 0) if getattr(ms, "sold_bot_raw", None) is not None else 0
    sold_ext = int(ms.sold_external_raw or 0) if getattr(ms, "sold_external_raw", None) is not None else 0
    assert sold_bot == 0
    assert sold_ext == sold_raw
    assert sold_bot + sold_ext == total_sold_steps

    # Second ingestion with the same tx history must be a no-op.
    ingested2 = _ingest_external_sells(state, rpc, wallet="WALLET", max_signatures=10, journal_path=None)
    assert ingested2 == 0
    assert len(ms.executed_steps) == 1
    # Remaining amount and trading_bag_raw unchanged.
    assert int(ms.lots[0].remaining_amount) == initial_amount - sold_raw
    assert int(ms.trading_bag_raw) == initial_amount - sold_raw

