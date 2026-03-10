from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mint_ladder_bot.models import (
    LotInfo,
    RpcInfo,
    RuntimeMintState,
    RuntimeState,
    SolBalance,
    StatusFile,
)
from mint_ladder_bot.reconciliation_report import compute_reconciliation_records


def _mk_state_and_status(
    mint: str,
    wallet_balance_raw: int,
    lots: list[LotInfo],
    sold_bot_raw: int = 0,
    sold_external_raw: int = 0,
) -> tuple[RuntimeState, StatusFile]:
    now = datetime.now(tz=timezone.utc)
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw=str(sum(int(l.remaining_amount) for l in lots)),
        moonbag_raw="0",
        lots=lots,
    )
    ms.sold_bot_raw = str(sold_bot_raw) if sold_bot_raw else None
    ms.sold_external_raw = str(sold_external_raw) if sold_external_raw else None
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


def test_reconciliation_sufficient_when_lots_explain_wallet():
    mint = "MINT_SUFFICIENT"
    wallet_raw = 1_000_000
    lot = LotInfo.create(
        mint=mint,
        token_amount_raw=wallet_raw,
        entry_price=1e-6,
        confidence="known",
        source="tx_parsed",
    )
    state, status = _mk_state_and_status(mint, wallet_raw, [lot])

    records = compute_reconciliation_records(state, status, mint_filter=mint)
    assert len(records) == 1
    rec = records[0]
    assert rec.wallet_balance_raw == wallet_raw
    assert rec.sum_active_lots_raw == wallet_raw
    assert rec.reconciliation_status == "sufficient"
    assert rec.blocker_category in ("other", "missing historical tx coverage")


def test_reconciliation_partial_when_small_residual():
    mint = "MINT_PARTIAL"
    wallet_raw = 1_000_000
    lot_amount = 820_000  # 18% residual
    lot = LotInfo.create(
        mint=mint,
        token_amount_raw=lot_amount,
        entry_price=1e-6,
        confidence="known",
        source="tx_parsed",
    )
    state, status = _mk_state_and_status(mint, wallet_raw, [lot])

    records = compute_reconciliation_records(state, status, mint_filter=mint)
    rec = records[0]
    assert rec.reconciliation_status == "partial"
    assert pytest.approx(rec.diff_pct, rel=1e-6) == (wallet_raw - lot_amount) / wallet_raw


def test_reconciliation_insufficient_when_large_residual():
    mint = "MINT_INSUFFICIENT"
    wallet_raw = 1_000_000
    lot_amount = 100_000  # 90% residual
    lot = LotInfo.create(
        mint=mint,
        token_amount_raw=lot_amount,
        entry_price=1e-6,
        confidence="known",
        source="tx_parsed",
    )
    state, status = _mk_state_and_status(mint, wallet_raw, [lot])

    records = compute_reconciliation_records(state, status, mint_filter=mint)
    rec = records[0]
    assert rec.reconciliation_status == "insufficient"
    assert rec.blocker_category == "missing historical tx coverage"


def test_bootstrap_only_classified_as_insufficient_and_bootstrap_category():
    mint = "MINT_BOOTSTRAP"
    wallet_raw = 1_000_000
    lot = LotInfo.create(
        mint=mint,
        token_amount_raw=500_000,
        entry_price=None,
        confidence="unknown",
        source="bootstrap_snapshot",
    )
    state, status = _mk_state_and_status(mint, wallet_raw, [lot])

    records = compute_reconciliation_records(state, status, mint_filter=mint)
    rec = records[0]
    assert rec.reconciliation_status == "insufficient"
    assert rec.bootstrap_lots_count == 1
    assert rec.tx_derived_lots_count == 0
    assert rec.blocker_category == "bootstrap-only inventory"


def test_reconciliation_report_is_deterministic():
    mint = "MINT_DETERMINISTIC"
    wallet_raw = 2_000_000
    lots = [
        LotInfo.create(
            mint=mint,
            token_amount_raw=1_000_000,
            entry_price=1e-6,
            confidence="known",
            source="tx_parsed",
        )
    ]
    state, status = _mk_state_and_status(mint, wallet_raw, lots)

    r1 = compute_reconciliation_records(state, status, mint_filter=mint)
    r2 = compute_reconciliation_records(state, status, mint_filter=mint)
    assert len(r1) == len(r2) == 1
    d1 = r1[0].to_dict()
    d2 = r2[0].to_dict()
    assert json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True)

