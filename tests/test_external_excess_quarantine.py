from datetime import datetime, timezone, timedelta

from mint_ladder_bot.config import Config
from mint_ladder_bot.models import FailureInfo, RuntimeMintState
from mint_ladder_bot.runner import (
    _compute_mint_holding_explanation,
    _trading_bag_from_lots,
    _update_reconciliation_pause_for_mint,
)
from mint_ladder_bot.dashboard_truth import token_truth
from mint_ladder_bot.models import MintStatus


def _mint_state_with_lot(balance_raw: int, lot_raw: int) -> RuntimeMintState:
    from mint_ladder_bot.models import LotInfo

    lot = LotInfo.create(mint="M", token_amount_raw=lot_raw, entry_price=1.0, source="tx_exact")
    lot.remaining_amount = str(lot_raw)
    ms = RuntimeMintState(
        entry_price_sol_per_token=1.0,
        trading_bag_raw=str(lot_raw),
        moonbag_raw="0",
    )
    ms.lots = [lot]
    ms.last_known_balance_raw = str(balance_raw)
    ms.failures = FailureInfo(count=0)
    return ms


def _status_for_mint(balance_raw: int) -> MintStatus:
    from mint_ladder_bot.models import RpcInfo, StatusFile

    # Minimal status with one mint; token_truth only needs per-mint fields.
    status = StatusFile(
        version=1,
        created_at=datetime.now(tz=timezone.utc),
        wallet="Wallet",
        rpc=RpcInfo(endpoint="http://example"),
        sol=None,
        mints=[
            MintStatus(
                mint="M",
                token_account="TokenAccount",
                decimals=6,
                balance_ui=balance_raw / 1e6,
                balance_raw=str(balance_raw),
                symbol="M",
                name="M",
            )
        ],
    )
    return status.mints[0]


def test_external_excess_quarantine_wallet_greater_than_tx_proven(monkeypatch):
    """
    Case 1: wallet_balance_raw >= tx_proven_raw > 0
    - mint must NOT be paused
    - external_excess_raw > 0
    - effective tradable amount == tx_proven_raw (from lots)
    """
    balance_raw = 1500
    lot_raw = 1000
    ms = _mint_state_with_lot(balance_raw, lot_raw)
    cfg = Config()

    now = datetime.now(tz=timezone.utc)
    expl = _compute_mint_holding_explanation(ms)
    assert expl["sum_active_lots"] == lot_raw

    # Simulate one reconciliation cycle using the helper directly (sum_lots <-> actual).
    _update_reconciliation_pause_for_mint(
        mint="M",
        mint_state=ms,
        actual_raw=balance_raw,
        sum_lots=lot_raw,
        now=now,
        config=cfg,
        event_journal_path=None,
    )
    # Under external-excess policy, runner should not mark reconciliation_mismatch pause
    # when wallet >= lots and lots > 0. The mint remains unpaused.
    assert ms.failures.last_error != "reconciliation_mismatch"

    # Trading bag comes from lots only; wallet excess does not increase sellable.
    tradable_from_lots = _trading_bag_from_lots(ms)
    assert tradable_from_lots == lot_raw


def test_external_excess_quarantine_wallet_less_than_tx_proven():
    """
    Case 2: tx_proven_raw > wallet_balance_raw
    - full reconciliation pause remains
    """
    balance_raw = 900
    lot_raw = 1000
    ms = _mint_state_with_lot(balance_raw, lot_raw)
    cfg = Config()

    now = datetime.now(tz=timezone.utc)
    for _ in range(3):
        _update_reconciliation_pause_for_mint(
            mint="M",
            mint_state=ms,
            actual_raw=balance_raw,
            sum_lots=lot_raw,
            now=now,
            config=cfg,
            event_journal_path=None,
        )
    assert ms.failures.last_error == "reconciliation_mismatch"
    assert ms.failures.paused_until is not None


def test_dashboard_truth_exposes_external_excess_fields(tmp_path):
    """
    Dashboard token_truth must expose tx_proven_raw, external_excess_raw, has_external_excess, external_excess_mode.
    """
    balance_raw = 1500
    lot_raw = 1000
    ms = _mint_state_with_lot(balance_raw, lot_raw)
    status_mint = _status_for_mint(balance_raw)

    mint_data = ms.dict()
    truth = token_truth("M", mint_data, status_mint_dict=status_mint.model_dump(), decimals=6, symbol="M")

    assert truth["tx_proven_raw"] == lot_raw
    assert truth["external_excess_raw"] == balance_raw - lot_raw
    assert truth["has_external_excess"] is True
    assert truth["external_excess_mode"] == "external_excess"

