from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from mint_ladder_bot.source_gap_diagnostics import (
    MintSourceGapReport,
    analyze_source_gap_for_mint,
)


def _mk_tx(signature: str, owner: str, mint: str, wallet_has_balance: bool) -> Dict[str, Any]:
    meta: Dict[str, Any] = {"preTokenBalances": [], "postTokenBalances": []}
    if wallet_has_balance:
        meta["postTokenBalances"].append(
            {"owner": owner, "mint": mint, "uiTokenAmount": {"amount": "100"}}
        )
    else:
        meta["postTokenBalances"].append(
            {"owner": "OTHER", "mint": mint, "uiTokenAmount": {"amount": "100"}}
        )
    return {
        "signature": signature,
        "slot": 1,
        "meta": meta,
    }


def test_source_gap_when_no_mentions():
    wallet = "WALLET"
    mint = "MINT"
    txs: List[Dict[str, Any]] = []  # empty corpus
    rpt: MintSourceGapReport = analyze_source_gap_for_mint(
        wallet=wallet,
        mint=mint,
        txs=txs,
        decimals_by_mint={mint: 6},
        symbol="SYM",
    )
    assert rpt.total_signatures_scanned == 0
    assert rpt.signatures_mentioning_mint_any == 0
    assert rpt.diagnosis_category == "source gap likely"


def test_transfer_provenance_when_only_non_wallet_balances(monkeypatch):
    wallet = "WALLET"
    mint = "MINT"
    # One tx mentioning mint, but owned by OTHER, not wallet.
    txs = [_mk_tx("SIG1", owner=wallet, mint=mint, wallet_has_balance=False)]

    # Force parsers to return no events regardless of tx content.
    def _fake_parse_buy_events_from_tx(tx, wallet, signature, mints_tracked, decimals_by_mint):
        return []

    def _fake_parse_sell_events_from_tx(tx, wallet, mints_tracked, signature):
        return []

    import mint_ladder_bot.source_gap_diagnostics as sg

    monkeypatch.setattr(sg, "_parse_buy_events_from_tx", _fake_parse_buy_events_from_tx)
    monkeypatch.setattr(sg, "parse_sell_events_from_tx", _fake_parse_sell_events_from_tx)

    rpt: MintSourceGapReport = analyze_source_gap_for_mint(
        wallet=wallet,
        mint=mint,
        txs=txs,
        decimals_by_mint={mint: 6},
        symbol="SYM",
    )
    assert rpt.signatures_mentioning_mint_any == 1
    assert rpt.signatures_with_wallet_mint_balances == 0
    assert rpt.diagnosis_category == "transfer provenance likely"


def test_parser_gap_when_wallet_balances_but_no_events(monkeypatch):
    wallet = "WALLET"
    mint = "MINT"
    # One tx where wallet owns the mint.
    txs = [_mk_tx("SIG1", owner=wallet, mint=mint, wallet_has_balance=True)]

    def _fake_parse_buy_events_from_tx(tx, wallet, signature, mints_tracked, decimals_by_mint):
        return []

    def _fake_parse_sell_events_from_tx(tx, wallet, mints_tracked, signature):
        return []

    import mint_ladder_bot.source_gap_diagnostics as sg

    monkeypatch.setattr(sg, "_parse_buy_events_from_tx", _fake_parse_buy_events_from_tx)
    monkeypatch.setattr(sg, "parse_sell_events_from_tx", _fake_parse_sell_events_from_tx)

    rpt: MintSourceGapReport = analyze_source_gap_for_mint(
        wallet=wallet,
        mint=mint,
        txs=txs,
        decimals_by_mint={mint: 6},
        symbol="SYM",
    )
    assert rpt.signatures_mentioning_mint_any == 1
    assert rpt.signatures_with_wallet_mint_balances == 1
    assert rpt.signatures_with_buy_events == 0
    assert rpt.signatures_with_sell_events == 0
    assert rpt.diagnosis_category == "parser gap likely"

