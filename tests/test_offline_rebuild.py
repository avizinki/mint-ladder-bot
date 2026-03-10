from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from mint_ladder_bot.models import (
    LotInfo,
    RuntimeMintState,
    RuntimeState,
    StatusFile,
    RpcInfo,
    SolBalance,
)
from mint_ladder_bot.offline_rebuild import (
    MintRebuildComparison,
    run_deep_rebuild_comparison,
    run_scratch_rebuild_for_mint,
)


class _FakeRpc:
    def __init__(self, signatures: List[str], txs: Dict[str, Dict[str, Any]]):
        self._sigs = signatures
        self._txs = txs

    def get_signatures_for_address(self, _addr: str, limit: int, before: str | None = None):
        return [{"signature": s} for s in self._sigs[:limit]]

    def get_transaction(self, signature: str) -> Dict[str, Any]:
        return self._txs.get(signature, {})


def _mk_status_and_state_for_mint(
    mint: str,
    wallet_balance_raw: int,
    lots_before: List[LotInfo],
) -> Tuple[StatusFile, RuntimeState]:
    now = datetime.now(tz=timezone.utc)
    status = StatusFile(
        version=1,
        created_at=now,
        wallet="WALLET",
        rpc=RpcInfo(endpoint="http://localhost", latency_ms=None),
        sol=SolBalance(lamports=0, sol=0.0),
        mints=[
            {
                "mint": mint,
                "token_account": "ACC",
                "decimals": 6,
                "balance_ui": wallet_balance_raw / 1e6,
                "balance_raw": str(wallet_balance_raw),
                "symbol": "SYM",
                "name": "Token",
                "entry": {
                    "mode": "auto",
                    "entry_price_sol_per_token": 1e-6,
                    "entry_source": "inferred_from_tx",
                    "entry_tx_signature": None,
                },
                "market": {},
            }
        ],
    )
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw=str(sum(int(l.remaining_amount) for l in lots_before)),
        moonbag_raw="0",
        lots=lots_before,
    )
    state = RuntimeState(
        version=1,
        started_at=now,
        status_file="status.json",
        wallet="WALLET",
        sol=SolBalance(lamports=0, sol=0.0),
        mints={mint: ms},
    )
    return status, state


def test_deeper_history_improves_explainability(monkeypatch):
    """
    Synthetic case: baseline has incomplete lots; scratch reconstruction from
    deeper history should improve reconciliation diff_pct for the mint.
    """
    mint = "MINT_DEEP"
    wallet_raw = 1_000_000

    # Baseline: only 200_000 tokens explained by lots.
    baseline_lot = LotInfo.create(
        mint=mint,
        token_amount_raw=200_000,
        entry_price=1e-6,
        confidence="known",
        source="tx_exact",
    )
    status, state_before = _mk_status_and_state_for_mint(
        mint=mint,
        wallet_balance_raw=wallet_raw,
        lots_before=[baseline_lot],
    )

    # Fake RPC and parser: emit one buy event covering the full wallet balance.
    sig = "SIG_BUY_FULL"
    rpc = _FakeRpc(signatures=[sig], txs={sig: {"meta": {}, "slot": 1}})

    from mint_ladder_bot.tx_lot_engine import BuyEvent
    import mint_ladder_bot.tx_lot_engine as txle

    def _fake_parse_buy_events_from_tx(tx: dict, wallet: str, signature: str, mints_tracked, decimals_by_mint):
        return [
            BuyEvent(
                signature=signature,
                mint=mint,
                token_amount_raw=wallet_raw,
                sol_spent_lamports=wallet_raw,
                entry_price_sol_per_token=1e-6,
                block_time=datetime.now(tz=timezone.utc),
            )
        ]

    monkeypatch.setattr(txle, "_parse_buy_events_from_tx", _fake_parse_buy_events_from_tx)

    decimals_by_mint = {mint: 6}
    symbol_by_mint = {mint: "SYM"}

    comp: MintRebuildComparison = run_deep_rebuild_comparison(
        status=status,
        state_before=state_before,
        wallet_pubkey="WALLET",
        mint=mint,
        rpc=rpc,
        max_signatures=10,
        decimals_by_mint=decimals_by_mint,
        symbol_by_mint=symbol_by_mint,
    )

    assert comp.diff_pct_before is not None
    assert comp.diff_pct_after is not None
    # Before: lots explain only part of wallet; after: explain nearly all.
    assert abs(comp.diff_pct_after) + 1e-9 < abs(comp.diff_pct_before)
    assert comp.improved is True


def test_scratch_rebuild_idempotent_for_same_history(monkeypatch):
    """
    Replaying the same deeper history for scratch rebuild must produce an
    identical scratch state (no duplicate lots or extra debits).
    """
    mint = "MINT_IDEMP"
    wallet_raw = 1_000_000

    status, state_before = _mk_status_and_state_for_mint(
        mint=mint,
        wallet_balance_raw=wallet_raw,
        lots_before=[],
    )

    sig = "SIG_IDEMP"
    rpc = _FakeRpc(signatures=[sig], txs={sig: {"meta": {}, "slot": 1}})

    from mint_ladder_bot.tx_lot_engine import BuyEvent
    import mint_ladder_bot.tx_lot_engine as txle

    def _fake_parse_buy_events_from_tx(tx: dict, wallet: str, signature: str, mints_tracked, decimals_by_mint):
        return [
            BuyEvent(
                signature=signature,
                mint=mint,
                token_amount_raw=wallet_raw,
                sol_spent_lamports=wallet_raw,
                entry_price_sol_per_token=1e-6,
                block_time=datetime.now(tz=timezone.utc),
            )
        ]

    monkeypatch.setattr(txle, "_parse_buy_events_from_tx", _fake_parse_buy_events_from_tx)

    decimals_by_mint = {mint: 6}
    symbol_by_mint = {mint: "SYM"}

    scratch1 = run_scratch_rebuild_for_mint(
        status=status,
        state_before=state_before,
        wallet_pubkey="WALLET",
        mint=mint,
        rpc=rpc,
        max_signatures=10,
        decimals_by_mint=decimals_by_mint,
        symbol_by_mint=symbol_by_mint,
    )
    scratch2 = run_scratch_rebuild_for_mint(
        status=status,
        state_before=state_before,
        wallet_pubkey="WALLET",
        mint=mint,
        rpc=rpc,
        max_signatures=10,
        decimals_by_mint=decimals_by_mint,
        symbol_by_mint=symbol_by_mint,
    )

    ms1 = scratch1.mints[mint]
    ms2 = scratch2.mints[mint]
    assert len(ms1.lots) == len(ms2.lots) == 1
    assert ms1.lots[0].remaining_amount == ms2.lots[0].remaining_amount
    assert ms1.trading_bag_raw == ms2.trading_bag_raw


def test_no_duplicate_lots_with_duplicate_signatures(monkeypatch):
    """
    When history contains duplicate signatures, scratch rebuild must not
    create duplicate lots for the same (signature, mint).
    """
    mint = "MINT_DUP_LOTS"
    wallet_raw = 1_000_000

    status, state_before = _mk_status_and_state_for_mint(
        mint=mint,
        wallet_balance_raw=wallet_raw,
        lots_before=[],
    )

    sig = "SIG_DUP"
    # Duplicate signatures in history.
    rpc = _FakeRpc(signatures=[sig, sig], txs={sig: {"meta": {}, "slot": 1}})

    from mint_ladder_bot.tx_lot_engine import BuyEvent
    import mint_ladder_bot.tx_lot_engine as txle

    def _fake_parse_buy_events_from_tx(tx: dict, wallet: str, signature: str, mints_tracked, decimals_by_mint):
        return [
            BuyEvent(
                signature=signature,
                mint=mint,
                token_amount_raw=wallet_raw,
                sol_spent_lamports=wallet_raw,
                entry_price_sol_per_token=1e-6,
                block_time=datetime.now(tz=timezone.utc),
            )
        ]

    monkeypatch.setattr(txle, "_parse_buy_events_from_tx", _fake_parse_buy_events_from_tx)

    decimals_by_mint = {mint: 6}
    symbol_by_mint = {mint: "SYM"}

    scratch = run_scratch_rebuild_for_mint(
        status=status,
        state_before=state_before,
        wallet_pubkey="WALLET",
        mint=mint,
        rpc=rpc,
        max_signatures=10,
        decimals_by_mint=decimals_by_mint,
        symbol_by_mint=symbol_by_mint,
    )

    ms = scratch.mints[mint]
    assert len(ms.lots) == 1


def test_no_duplicate_debits_with_external_sells(monkeypatch):
    """
    Deeper backfill that includes external sells must not introduce duplicate
    debits for the same (mint, signature).
    """
    mint = "MINT_DUP_SELL"
    wallet_raw = 1_000_000

    # Baseline empty; scratch will create one buy then one external sell.
    status, state_before = _mk_status_and_state_for_mint(
        mint=mint,
        wallet_balance_raw=wallet_raw,
        lots_before=[],
    )

    sig = "SIG_SELL"
    rpc = _FakeRpc(signatures=[sig], txs={sig: {"meta": {}, "slot": 1}})

    from mint_ladder_bot.tx_lot_engine import BuyEvent
    import mint_ladder_bot.tx_lot_engine as txle
    import mint_ladder_bot.runner as rmod

    buy_amount = 1_000_000
    sold_raw = 400_000

    def _fake_parse_buy_events_from_tx(tx: dict, wallet: str, signature: str, mints_tracked, decimals_by_mint):
        return [
            BuyEvent(
                signature=signature,
                mint=mint,
                token_amount_raw=buy_amount,
                sol_spent_lamports=buy_amount,
                entry_price_sol_per_token=1e-6,
                block_time=datetime.now(tz=timezone.utc),
            )
        ]

    class _FakeSellEvent:
        def __init__(self, mint: str, sold_raw: int, sol_in_lamports: int):
            self.mint = mint
            self.sold_raw = sold_raw
            self.sol_in_lamports = sol_in_lamports
            self.block_time = None

    def _fake_parse_sell_events_from_tx(tx: dict, wallet: str, mints_tracked, signature: str):
        return [_FakeSellEvent(mint=mint, sold_raw=sold_raw, sol_in_lamports=1_000_000_000)]

    monkeypatch.setattr(txle, "_parse_buy_events_from_tx", _fake_parse_buy_events_from_tx)
    monkeypatch.setattr(rmod, "parse_sell_events_from_tx", _fake_parse_sell_events_from_tx)

    decimals_by_mint = {mint: 6}
    symbol_by_mint = {mint: "SYM"}

    # First scratch rebuild.
    scratch1 = run_scratch_rebuild_for_mint(
        status=status,
        state_before=state_before,
        wallet_pubkey="WALLET",
        mint=mint,
        rpc=rpc,
        max_signatures=10,
        decimals_by_mint=decimals_by_mint,
        symbol_by_mint=symbol_by_mint,
    )
    ms1 = scratch1.mints[mint]
    assert len(ms1.lots) == 1
    assert int(ms1.lots[0].remaining_amount) == buy_amount - sold_raw

    # Second scratch rebuild with same history should not double-debit.
    scratch2 = run_scratch_rebuild_for_mint(
        status=status,
        state_before=state_before,
        wallet_pubkey="WALLET",
        mint=mint,
        rpc=rpc,
        max_signatures=10,
        decimals_by_mint=decimals_by_mint,
        symbol_by_mint=symbol_by_mint,
    )
    ms2 = scratch2.mints[mint]
    assert len(ms2.lots) == 1
    assert int(ms2.lots[0].remaining_amount) == buy_amount - sold_raw

