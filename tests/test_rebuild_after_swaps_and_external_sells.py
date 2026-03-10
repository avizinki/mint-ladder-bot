from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List

from mint_ladder_bot.models import LotInfo, RuntimeMintState, RuntimeState
from mint_ladder_bot.runner import _ingest_external_sells
from mint_ladder_bot.tx_lot_engine import BuyEvent, run_tx_first_lot_engine


class _FakeRpc:
    """
    Minimal fake RpcClient for replay/rebuild tests with mixed events.

    Presents a fixed ordered history of three signatures:
    1) SOL -> token buy
    2) token -> token swap (second buy for same mint)
    3) external sell of that mint
    """

    def __init__(self, signatures: List[str], txs: Dict[str, Dict[str, Any]]):
        self._sigs = signatures
        self._txs = txs

    def get_signatures_for_address(self, _addr: str, limit: int, before: str | None = None):
        # Pagination semantics are not important for this deterministic test; return a prefix.
        return [{"signature": s} for s in self._sigs[:limit]]

    def get_transaction(self, signature: str) -> Dict[str, Any]:
        return self._txs.get(signature, {})


@dataclass
class _FakeSellEvent:
    mint: str
    sold_raw: int
    sol_in_lamports: int
    block_time: datetime | None = None


def _projection(state: RuntimeState, mint: str) -> Dict[str, Any]:
    ms = state.mints[mint]
    lots = getattr(ms, "lots", None) or []
    return {
        "lot_count": len(lots),
        "lots_remaining": sorted(int(getattr(l, "remaining_amount", 0) or 0) for l in lots),
        "sold_bot_raw": int(getattr(ms, "sold_bot_raw", 0) or 0),
        "sold_external_raw": int(getattr(ms, "sold_external_raw", 0) or 0),
        "trading_bag_raw": int(ms.trading_bag_raw),
        # Wallet balance proxy: last_known_balance_raw if set, else trading_bag_raw.
        "wallet_balance_raw": int(getattr(ms, "last_known_balance_raw", None) or ms.trading_bag_raw),
    }


def _build_state_from_history(rpc: _FakeRpc, wallet: str, mint: str) -> RuntimeState:
    # Start from a clean RuntimeState as a fresh runtime would after restart.
    state = RuntimeState(
        started_at=datetime.now(tz=timezone.utc),
        status_file="status.json",
        mints={},
    )

    # Track the target mint so tx-first engine knows which mint(s) to build.
    state.mints[mint] = RuntimeMintState(
        entry_price_sol_per_token=0.0,
        trading_bag_raw="0",
        moonbag_raw="0",
    )

    # Monkeypatch buy and sell parsers to emit a controlled mixed event sequence.
    import mint_ladder_bot.tx_lot_engine as txle
    import mint_ladder_bot.runner as runner_mod

    sig_buy = "SIG_BUY_SOL"
    sig_swap = "SIG_SWAP_TT"
    sig_ext = "SIG_EXT_SELL"

    def _fake_parse_buy_events_from_tx(tx: dict, wallet_addr: str, signature: str, mints_tracked, decimals_by_mint):
        # Only care about a single tracked mint for this test.
        assert wallet_addr == wallet
        events: List[BuyEvent] = []
        now = datetime.now(tz=timezone.utc)
        if signature == sig_buy:
            # SOL -> token buy: 1_000_000 raw
            events.append(
                BuyEvent(
                    signature=signature,
                    mint=mint,
                    token_amount_raw=1_000_000,
                    sol_spent_lamports=1_000_000_000,
                    entry_price_sol_per_token=1e-6,
                    block_time=now,
                )
            )
        elif signature == sig_swap:
            # token -> token swap that yields more of the same mint: +500_000 raw
            events.append(
                BuyEvent(
                    signature=signature,
                    mint=mint,
                    token_amount_raw=500_000,
                    sol_spent_lamports=0,
                    entry_price_sol_per_token=2e-6,
                    block_time=now,
                )
            )
        return events

    def _fake_parse_sell_events_from_tx(tx: dict, wallet_addr: str, mints_tracked, signature: str):
        assert wallet_addr == wallet
        if signature != sig_ext:
            return []
        # External sell of 400_000 units of the same mint.
        return [
            _FakeSellEvent(
                mint=mint,
                sold_raw=400_000,
                sol_in_lamports=500_000_000,
                block_time=datetime.now(tz=timezone.utc),
            )
        ]

    # Apply monkeypatches.
    txle._orig_parse_buy_events_from_tx = getattr(txle, "_parse_buy_events_from_tx", None)
    runner_mod._orig_parse_sell_events = getattr(runner_mod, "parse_sell_events_from_tx", None)
    txle._parse_buy_events_from_tx = _fake_parse_buy_events_from_tx  # type: ignore[assignment]
    runner_mod.parse_sell_events_from_tx = _fake_parse_sell_events_from_tx  # type: ignore[assignment]

    try:
        # First pass: tx-first engine builds lots from SOL buy + token->token swap.
        decimals_by_mint = {mint: 6}
        run_tx_first_lot_engine(
            state=state,
            rpc=rpc,
            wallet_pubkey=wallet,
            decimals_by_mint=decimals_by_mint,
            journal_path=None,
            max_signatures=10,
        )

        # Sanity: we expect two lots (one per buy event) and a trading bag equal to their sum.
        ms = state.mints[mint]
        lots = getattr(ms, "lots", None) or []
        assert len(lots) == 2
        total_raw = sum(int(getattr(l, "remaining_amount", 0) or 0) for l in lots)
        assert total_raw == 1_500_000
        # In production, trading_bag_raw is derived from lots; mirror that here.
        ms.trading_bag_raw = str(total_raw)

        # Second pass: ingest an external sell from the same wallet history.
        _ingest_external_sells(
            state=state,
            rpc=rpc,
            wallet=wallet,
            max_signatures=10,
            journal_path=None,
        )

        # After external sell of 400_000, FIFO debits should leave 1_100_000 remaining.
        ms = state.mints[mint]
        lots = getattr(ms, "lots", None) or []
        remaining_total = sum(int(getattr(l, "remaining_amount", 0) or 0) for l in lots)
        assert remaining_total == 1_100_000
        assert int(ms.trading_bag_raw) == 1_100_000
        # Track last-known wallet balance for projection equality.
        ms.last_known_balance_raw = str(remaining_total)

        return state
    finally:
        # Restore original parsers to avoid side effects on other tests.
        if getattr(txle, "_orig_parse_buy_events_from_tx", None) is not None:
            txle._parse_buy_events_from_tx = txle._orig_parse_buy_events_from_tx  # type: ignore[assignment]
            del txle._orig_parse_buy_events_from_tx
        if getattr(runner_mod, "_orig_parse_sell_events", None) is not None:
            runner_mod.parse_sell_events_from_tx = runner_mod._orig_parse_sell_events  # type: ignore[assignment]
            del runner_mod._orig_parse_sell_events


def test_rebuild_after_mixed_events_is_economically_identical():
    """
    Full replay after a mixed event history (buy, token->token swap, external sell)
    must produce the same economic state as the original run.

    Event sequence (all for the same mint):
    1. SOL -> token buy: creates first tx-derived lot (1_000_000 raw)
    2. token -> token swap: creates second tx-derived lot (500_000 raw)
    3. external sell: debits 400_000 raw via FIFO

    Invariant: for the tracked mint, the following must match before/after rebuild:
    - lot_count
    - lots_remaining (per-lot remaining_amounts)
    - sold_bot_raw
    - sold_external_raw
    - trading_bag_raw
    - wallet_balance_raw (proxy from last_known_balance_raw)
    """

    mint = "MINT_REBUILD_MIXED"
    wallet = "WALLET_REBUILD_MIXED"

    sigs = ["SIG_BUY_SOL", "SIG_SWAP_TT", "SIG_EXT_SELL"]
    # Transaction contents are not inspected by the fake parsers; keep them minimal.
    txs: Dict[str, Dict[str, Any]] = {sig: {"meta": {}, "slot": i + 1} for i, sig in enumerate(sigs)}
    rpc = _FakeRpc(signatures=sigs, txs=txs)

    # First run: represents state before restart.
    state1 = _build_state_from_history(rpc, wallet=wallet, mint=mint)
    proj1 = _projection(state1, mint)

    # "Runtime restart": start from a clean state and replay the same on-chain history.
    state2 = _build_state_from_history(rpc, wallet=wallet, mint=mint)
    proj2 = _projection(state2, mint)

    assert proj1 == proj2

