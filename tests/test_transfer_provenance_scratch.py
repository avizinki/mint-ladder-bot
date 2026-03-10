"""
Deterministic tests for Step 3: scratch reconstruction for one trusted source wallet.

No state mutation; scratch-only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from mint_ladder_bot.models import LotInfo, RuntimeMintState, RuntimeState, StatusFile
from mint_ladder_bot.transfer_provenance_scratch import (
    PROPOSED_TRUSTED_TRANSFER_DERIVED,
    REMAINS_UNTRUSTED_OR_NONTRADABLE,
    SRC_RECON_FAILED,
    SRC_RECON_INSUFFICIENT,
    SRC_RECON_SUCCESS,
    _fifo_attribution,
    _provenance_valid_lots_ordered,
    run_scratch_trusted_transfer_analysis,
)


def _make_status(wallet: str, mint: str, balance_raw: int = 1000) -> StatusFile:
    from mint_ladder_bot.models import (
        DexscreenerMarketInfo,
        EntryInfo,
        MarketInfo,
        MintStatus,
        RpcInfo,
        SolBalance,
    )
    return StatusFile(
        version=1,
        created_at=datetime.now(tz=timezone.utc),
        wallet=wallet,
        rpc=RpcInfo(endpoint="", latency_ms=None),
        sol=SolBalance(lamports=0, sol=0.0),
        mints=[
            MintStatus(
                mint=mint,
                token_account="",
                decimals=6,
                balance_ui=balance_raw / 1e6,
                balance_raw=str(balance_raw),
                symbol="TKN",
                name="Token",
                entry=EntryInfo(),
                market=MarketInfo(dexscreener=DexscreenerMarketInfo()),
            )
        ],
    )


def _make_state(wallet: str, mint: str, sum_lots: int = 0, lots: list = None) -> RuntimeState:
    lots = lots or []
    ms = RuntimeMintState(
        entry_price_sol_per_token=1e-6,
        trading_bag_raw=str(sum_lots),
        moonbag_raw="0",
        lots=lots,
    )
    return RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file="",
        wallet=wallet,
        sol=None,
        mints={mint: ms},
    )


def test_fifo_attribution_full_cover():
    """FIFO attribution covers full amount with valid entry prices."""
    lot = LotInfo.create(
        mint="M",
        token_amount_raw=500,
        entry_price=1e-6,
        source="tx_exact",
    )
    lot.remaining_amount = "500"
    ordered = [(lot, 500)]
    attributed, cost, ok = _fifo_attribution(ordered, 500, 6)
    assert ok is True
    assert attributed == 500
    assert cost > 0


def test_fifo_attribution_capped():
    """Propagated amount capped by valid source lots."""
    lot = LotInfo.create(
        mint="M",
        token_amount_raw=100,
        entry_price=1e-6,
        source="tx_exact",
    )
    lot.remaining_amount = "100"
    ordered = [(lot, 100)]
    attributed, cost, ok = _fifo_attribution(ordered, 200, 6)
    assert ok is False
    assert attributed == 100


def test_provenance_valid_ordered_excludes_non_tx():
    """Only tx_exact/tx_parsed lots count."""
    lot_tx = LotInfo.create(mint="M", token_amount_raw=100, source="tx_exact")
    lot_tx.remaining_amount = "100"
    lot_boot = LotInfo.create(mint="M", token_amount_raw=50, source="bootstrap_snapshot")
    lot_boot.remaining_amount = "50"
    ms = RuntimeMintState(
        entry_price_sol_per_token=0,
        trading_bag_raw="150",
        moonbag_raw="0",
        lots=[lot_tx, lot_boot],
    )
    ordered = _provenance_valid_lots_ordered(ms)
    assert len(ordered) == 1
    assert ordered[0][1] == 100


def test_untrusted_source_rejected():
    """Untrusted source wallet -> remains_untrusted_or_nontradable."""
    dest_wallet = "Dest"
    source_wallet = "Untrusted"
    mint = "Mint1"
    status = _make_status(dest_wallet, mint, balance_raw=500)
    state = _make_state(dest_wallet, mint, sum_lots=0)

    report = run_scratch_trusted_transfer_analysis(
        destination_wallet=dest_wallet,
        destination_state=state,
        status=status,
        mint=mint,
        source_wallet=source_wallet,
        tx_signature="sig1",
        transferred_amount_raw=500,
        trusted_source_wallets=[],  # none trusted
        rpc=None,
        max_signatures=10,
        decimals_by_mint={mint: 6},
    )
    assert report.proposed_classification == REMAINS_UNTRUSTED_OR_NONTRADABLE
    assert report.why == "source_wallet_not_in_trusted_source_wallets"
    assert report.source_reconstruction_status == SRC_RECON_FAILED


def test_trusted_insufficient_inventory_reject():
    """Trusted source but insufficient valid inventory -> reject (mocked)."""
    dest_wallet = "Dest"
    source_wallet = "Trusted"
    mint = "Mint1"
    status = _make_status(dest_wallet, mint, balance_raw=500)
    state = _make_state(dest_wallet, mint, sum_lots=0)

    def _mock_source_recon(*args, **kwargs):
        scratch = RuntimeState(
            version=1,
            started_at=datetime.now(tz=timezone.utc),
            status_file="",
            wallet=source_wallet,
            sol=None,
            mints={
                mint: RuntimeMintState(
                    entry_price_sol_per_token=0,
                    trading_bag_raw="0",
                    moonbag_raw="0",
                    lots=[],  # no tx-derived lots
                )
            },
        )
        return scratch, SRC_RECON_INSUFFICIENT

    with patch(
        "mint_ladder_bot.transfer_provenance_scratch.run_source_wallet_scratch_reconstruction",
        side_effect=_mock_source_recon,
    ):
        report = run_scratch_trusted_transfer_analysis(
            destination_wallet=dest_wallet,
            destination_state=state,
            status=status,
            mint=mint,
            source_wallet=source_wallet,
            tx_signature="sig1",
            transferred_amount_raw=500,
            trusted_source_wallets=[source_wallet],
            rpc=None,
            max_signatures=10,
            decimals_by_mint={mint: 6},
        )
    assert report.proposed_classification == REMAINS_UNTRUSTED_OR_NONTRADABLE
    assert report.source_reconstruction_status == SRC_RECON_INSUFFICIENT


def test_trusted_valid_upstream_proposed_lot():
    """Trusted source with valid upstream buy -> proposed_trusted_transfer_derived."""
    dest_wallet = "Dest"
    source_wallet = "Trusted"
    mint = "Mint1"
    status = _make_status(dest_wallet, mint, balance_raw=500)
    state = _make_state(dest_wallet, mint, sum_lots=0)

    source_lot = LotInfo.create(
        mint=mint,
        token_amount_raw=500,
        entry_price=1e-6,
        source="tx_exact",
    )
    source_lot.remaining_amount = "500"

    def _mock_source_recon(*args, **kwargs):
        scratch = RuntimeState(
            version=1,
            started_at=datetime.now(tz=timezone.utc),
            status_file="",
            wallet=source_wallet,
            sol=None,
            mints={
                mint: RuntimeMintState(
                    entry_price_sol_per_token=1e-6,
                    trading_bag_raw="500",
                    moonbag_raw="0",
                    lots=[source_lot],
                )
            },
        )
        return scratch, SRC_RECON_SUCCESS

    with patch(
        "mint_ladder_bot.transfer_provenance_scratch.run_source_wallet_scratch_reconstruction",
        side_effect=_mock_source_recon,
    ):
        report = run_scratch_trusted_transfer_analysis(
            destination_wallet=dest_wallet,
            destination_state=state,
            status=status,
            mint=mint,
            source_wallet=source_wallet,
            tx_signature="sig1",
            transferred_amount_raw=500,
            trusted_source_wallets=[source_wallet],
            rpc=None,
            max_signatures=10,
            decimals_by_mint={mint: 6},
        )
    assert report.proposed_classification == PROPOSED_TRUSTED_TRANSFER_DERIVED
    assert report.amount_propagated_raw == 500
    assert report.proposed_entry_price_sol_per_token is not None
    assert report.proposed_lot_in_memory is not None
    assert report.proposed_lot_in_memory.source == "trusted_transfer_derived"
    assert report.before["sum_active_lots_raw"] == 0
    assert report.after["sum_active_lots_raw"] == 500


def test_propagated_capped_by_valid():
    """Propagated amount capped by valid source inventory."""
    dest_wallet = "Dest"
    source_wallet = "Trusted"
    mint = "Mint1"
    status = _make_status(dest_wallet, mint, balance_raw=200)
    state = _make_state(dest_wallet, mint, sum_lots=0)

    source_lot = LotInfo.create(
        mint=mint,
        token_amount_raw=100,
        entry_price=1e-6,
        source="tx_exact",
    )
    source_lot.remaining_amount = "100"

    def _mock_source_recon(*args, **kwargs):
        scratch = RuntimeState(
            version=1,
            started_at=datetime.now(tz=timezone.utc),
            status_file="",
            wallet=source_wallet,
            sol=None,
            mints={
                mint: RuntimeMintState(
                    entry_price_sol_per_token=1e-6,
                    trading_bag_raw="100",
                    moonbag_raw="0",
                    lots=[source_lot],
                )
            },
        )
        return scratch, SRC_RECON_SUCCESS

    with patch(
        "mint_ladder_bot.transfer_provenance_scratch.run_source_wallet_scratch_reconstruction",
        side_effect=_mock_source_recon,
    ):
        report = run_scratch_trusted_transfer_analysis(
            destination_wallet=dest_wallet,
            destination_state=state,
            status=status,
            mint=mint,
            source_wallet=source_wallet,
            tx_signature="sig1",
            transferred_amount_raw=200,  # more than source has
            trusted_source_wallets=[source_wallet],
            rpc=None,
            max_signatures=10,
            decimals_by_mint={mint: 6},
        )
    # FIFO can't cover 200 -> reject
    assert report.proposed_classification == REMAINS_UNTRUSTED_OR_NONTRADABLE
    assert report.why == "fifo_attribution_insufficient_or_invalid_entry"


def test_same_input_same_output():
    """Same inputs -> same proposed output (deterministic)."""
    dest_wallet = "Dest"
    source_wallet = "Trusted"
    mint = "Mint1"
    status = _make_status(dest_wallet, mint, balance_raw=500)
    state = _make_state(dest_wallet, mint, sum_lots=0)
    source_lot = LotInfo.create(mint=mint, token_amount_raw=500, entry_price=1e-6, source="tx_exact")
    source_lot.remaining_amount = "500"

    def _mock_recon(*args, **kwargs):
        scratch = RuntimeState(
            version=1,
            started_at=datetime.now(tz=timezone.utc),
            status_file="",
            wallet=source_wallet,
            sol=None,
            mints={
                mint: RuntimeMintState(
                    entry_price_sol_per_token=1e-6,
                    trading_bag_raw="500",
                    moonbag_raw="0",
                    lots=[source_lot],
                )
            },
        )
        return scratch, SRC_RECON_SUCCESS

    with patch(
        "mint_ladder_bot.transfer_provenance_scratch.run_source_wallet_scratch_reconstruction",
        side_effect=_mock_recon,
    ):
        r1 = run_scratch_trusted_transfer_analysis(
            destination_wallet=dest_wallet,
            destination_state=state,
            status=status,
            mint=mint,
            source_wallet=source_wallet,
            tx_signature="sig1",
            transferred_amount_raw=500,
            trusted_source_wallets=[source_wallet],
            rpc=None,
            max_signatures=10,
            decimals_by_mint={mint: 6},
        )
        r2 = run_scratch_trusted_transfer_analysis(
            destination_wallet=dest_wallet,
            destination_state=state,
            status=status,
            mint=mint,
            source_wallet=source_wallet,
            tx_signature="sig1",
            transferred_amount_raw=500,
            trusted_source_wallets=[source_wallet],
            rpc=None,
            max_signatures=10,
            decimals_by_mint={mint: 6},
        )
    assert r1.proposed_classification == r2.proposed_classification == PROPOSED_TRUSTED_TRANSFER_DERIVED
    assert r1.amount_propagated_raw == r2.amount_propagated_raw == 500


def test_before_after_reported():
    """Before/after comparison is reported."""
    dest_wallet = "Dest"
    source_wallet = "Trusted"
    mint = "Mint1"
    status = _make_status(dest_wallet, mint, balance_raw=500)
    existing_lot = LotInfo.create(mint=mint, token_amount_raw=100, entry_price=1e-6, source="tx_exact")
    existing_lot.remaining_amount = "100"
    state = _make_state(dest_wallet, mint, sum_lots=100, lots=[existing_lot])

    source_lot = LotInfo.create(mint=mint, token_amount_raw=500, entry_price=1e-6, source="tx_exact")
    source_lot.remaining_amount = "500"

    def _mock_recon(*args, **kwargs):
        scratch = RuntimeState(
            version=1,
            started_at=datetime.now(tz=timezone.utc),
            status_file="",
            wallet=source_wallet,
            sol=None,
            mints={
                mint: RuntimeMintState(
                    entry_price_sol_per_token=1e-6,
                    trading_bag_raw="500",
                    moonbag_raw="0",
                    lots=[source_lot],
                )
            },
        )
        return scratch, SRC_RECON_SUCCESS

    with patch(
        "mint_ladder_bot.transfer_provenance_scratch.run_source_wallet_scratch_reconstruction",
        side_effect=_mock_recon,
    ):
        report = run_scratch_trusted_transfer_analysis(
            destination_wallet=dest_wallet,
            destination_state=state,
            status=status,
            mint=mint,
            source_wallet=source_wallet,
            tx_signature="sig1",
            transferred_amount_raw=500,
            trusted_source_wallets=[source_wallet],
            rpc=None,
            max_signatures=10,
            decimals_by_mint={mint: 6},
        )
    assert "sum_active_lots_raw" in report.before
    assert "sum_active_lots_raw" in report.after
    assert report.before["sum_active_lots_raw"] == 100
    assert report.after["sum_active_lots_raw"] == 600  # 100 existing + 500 proposed


def test_no_state_mutation():
    """Destination state is not mutated."""
    dest_wallet = "Dest"
    source_wallet = "Trusted"
    mint = "Mint1"
    status = _make_status(dest_wallet, mint, balance_raw=500)
    state = _make_state(dest_wallet, mint, sum_lots=0)
    initial_lots = len(state.mints[mint].lots)

    def _mock_recon(*args, **kwargs):
        scratch = RuntimeState(
            version=1,
            started_at=datetime.now(tz=timezone.utc),
            status_file="",
            wallet=source_wallet,
            sol=None,
            mints={
                mint: RuntimeMintState(
                    entry_price_sol_per_token=1e-6,
                    trading_bag_raw="500",
                    moonbag_raw="0",
                    lots=[
                        LotInfo.create(
                            mint=mint,
                            token_amount_raw=500,
                            entry_price=1e-6,
                            source="tx_exact",
                        )
                    ],
                )
            },
        )
        scratch.mints[mint].lots[0].remaining_amount = "500"
        return scratch, SRC_RECON_SUCCESS

    with patch(
        "mint_ladder_bot.transfer_provenance_scratch.run_source_wallet_scratch_reconstruction",
        side_effect=_mock_recon,
    ):
        run_scratch_trusted_transfer_analysis(
            destination_wallet=dest_wallet,
            destination_state=state,
            status=status,
            mint=mint,
            source_wallet=source_wallet,
            tx_signature="sig1",
            transferred_amount_raw=500,
            trusted_source_wallets=[source_wallet],
            rpc=None,
            max_signatures=10,
            decimals_by_mint={mint: 6},
        )
    assert len(state.mints[mint].lots) == initial_lots
    assert state.mints[mint].trading_bag_raw == "0"
