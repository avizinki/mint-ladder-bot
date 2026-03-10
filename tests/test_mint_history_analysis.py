from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from mint_ladder_bot.mint_history_analysis import analyze_mint_history
from mint_ladder_bot.history_checkpoint import HistoryCheckpoint
from mint_ladder_bot.models import (
    LotInfo,
    RpcInfo,
    RuntimeMintState,
    RuntimeState,
    SolBalance,
    StatusFile,
)


def _mk_state_and_status(
    mint: str,
    wallet_balance_raw: int,
    lots: List[LotInfo],
) -> Tuple[RuntimeState, StatusFile]:
    now = datetime.now(tz=timezone.utc)
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw=str(sum(int(l.remaining_amount) for l in lots)),
        moonbag_raw="0",
        lots=lots,
    )
    state = RuntimeState(
        version=1,
        started_at=now,
        status_file="status.json",
        mints={mint: ms},
    )
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
    return state, status


def test_mint_relevance_positive_counts_signatures():
    mint = "MINT_RELEVANT"
    wallet = "WALLET"
    wallet_raw = 1_000_000
    lot = LotInfo.create(
        mint=mint,
        token_amount_raw=200_000,
        entry_price=1e-6,
        confidence="known",
        source="tx_parsed",
    )
    state, status = _mk_state_and_status(mint, wallet_raw, [lot])

    pages: List[Sequence[Dict]] = [
        [
            {
                "signature": "sig1",
                "slot": 10,
                "meta": {
                    "preTokenBalances": [],
                    "postTokenBalances": [
                        {"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "100000"}},
                    ],
                },
            },
            {
                "signature": "sig2",
                "slot": 9,
                "meta": {"preTokenBalances": [], "postTokenBalances": []},
            },
        ]
    ]

    def fetch_txs(before: Optional[str], limit: int) -> Sequence[Dict]:
        # Single-page history; ignore before/limit.
        return pages[0] if before is None else []

    res = analyze_mint_history(
        state=state,
        status=status,
        wallet=wallet,
        mint=mint,
        fetch_txs=fetch_txs,
        initial_checkpoint=None,
        max_pages=5,
        page_limit=10,
    )

    assert res.pages_scanned == 1
    assert res.signatures_scanned == 2
    assert res.mint_relevant_signatures == 1


def test_mint_relevance_zero_when_no_matching_entries():
    mint = "MINT_NONE"
    wallet = "WALLET"
    wallet_raw = 1_000_000
    lot = LotInfo.create(
        mint=mint,
        token_amount_raw=500_000,
        entry_price=1e-6,
        confidence="known",
        source="tx_parsed",
    )
    state, status = _mk_state_and_status(mint, wallet_raw, [lot])

    def fetch_txs(before: Optional[str], limit: int) -> Sequence[Dict]:
        return [
            {
                "signature": "sigX",
                "slot": 10,
                "meta": {
                    "preTokenBalances": [],
                    "postTokenBalances": [
                        # Different mint; should not be counted.
                        {"owner": wallet, "mint": "OTHER", "uiTokenAmount": {"amount": "100000"}},
                    ],
                },
            }
        ]

    res = analyze_mint_history(
        state=state,
        status=status,
        wallet=wallet,
        mint=mint,
        fetch_txs=fetch_txs,
        initial_checkpoint=None,
        max_pages=5,
        page_limit=10,
    )

    assert res.mint_relevant_signatures == 0


def test_analysis_deterministic_for_same_pages():
    mint = "MINT_DETERMINISTIC"
    wallet = "WALLET"
    wallet_raw = 1_000_000
    lot = LotInfo.create(
        mint=mint,
        token_amount_raw=500_000,
        entry_price=1e-6,
        confidence="known",
        source="tx_parsed",
    )
    state, status = _mk_state_and_status(mint, wallet_raw, [lot])

    pages: List[Sequence[Dict]] = [
        [
            {
                "signature": "sig1",
                "slot": 10,
                "meta": {
                    "preTokenBalances": [],
                    "postTokenBalances": [
                        {"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "100000"}},
                    ],
                },
            }
        ]
    ]

    def fetch_txs(before: Optional[str], limit: int) -> Sequence[Dict]:
        return pages[0] if before is None else []

    res1 = analyze_mint_history(
        state=state,
        status=status,
        wallet=wallet,
        mint=mint,
        fetch_txs=fetch_txs,
        initial_checkpoint=None,
        max_pages=5,
        page_limit=10,
    )
    res2 = analyze_mint_history(
        state=state,
        status=status,
        wallet=wallet,
        mint=mint,
        fetch_txs=fetch_txs,
        initial_checkpoint=None,
        max_pages=5,
        page_limit=10,
    )

    assert res1.to_dict() == res2.to_dict()


def test_analysis_handles_exhausted_scan_cleanly():
    mint = "MINT_EMPTY"
    wallet = "WALLET"
    wallet_raw = 0
    lot = LotInfo.create(
        mint=mint,
        token_amount_raw=0,
        entry_price=None,
        confidence="unknown",
        source="tx_parsed",
    )
    state, status = _mk_state_and_status(mint, wallet_raw, [lot])

    def fetch_txs(before: Optional[str], limit: int) -> Sequence[Dict]:
        return []

    res = analyze_mint_history(
        state=state,
        status=status,
        wallet=wallet,
        mint=mint,
        fetch_txs=fetch_txs,
        initial_checkpoint=None,
        max_pages=5,
        page_limit=10,
    )

    assert res.pages_scanned == 0
    assert res.signatures_scanned == 0
    assert res.exhausted is True
    assert res.mint_relevant_signatures == 0

