from datetime import datetime, timezone

from mint_ladder_bot.config import Config
from mint_ladder_bot.dashboard_server import build_dashboard_payload
from mint_ladder_bot.models import ManualOverrideRecord, RuntimeMintState, RuntimeState, SolBalance
from mint_ladder_bot.reconciliation_report import compute_reconciliation_records


def _make_state_with_override(tmp_path, enable_override: bool = False, allow_mint: bool = False):
    mint = "MintManualOverrideTest"
    state = RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file=str(tmp_path / "status.json"),
        wallet="WalletManualOverrideTest",
        sol=SolBalance(lamports=0, sol=0.0),
        mints={},
    )
    ms = RuntimeMintState(
        entry_price_sol_per_token=1.0,
        trading_bag_raw="0",
        moonbag_raw="0",
    )
    ms.last_known_balance_raw = "1000"
    ms.manual_override_inventory.append(
        ManualOverrideRecord(
            mint=mint,
            symbol="OVR",
            amount_raw=400,
            reason="legacy holdings",
            provenance_note="legacy/manual",
            operator_approved=True,
        )
    )
    state.mints[mint] = ms
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())

    # Minimal status with same mint/balance so reconciliation can run
    from mint_ladder_bot.models import MintStatus, RpcInfo, StatusFile

    status = StatusFile(
        version=1,
        created_at=datetime.now(tz=timezone.utc),
        wallet=state.wallet,
        rpc=RpcInfo(endpoint="http://example"),
        sol=state.sol,
        mints=[
            MintStatus(
                mint=mint,
                token_account="TokenAccountManualOverride",
                decimals=6,
                balance_ui=0.0,
                balance_raw="1000",
                symbol="OVR",
                name="Override",
            )
        ],
    )
    status_path = tmp_path / "status.json"
    status_path.write_text(status.model_dump_json())

    # Config flags (env-driven in real runtime). Here we just assert the wiring exists.
    cfg = Config()
    cfg.enable_manual_override_inventory = enable_override
    cfg.manual_override_allowed_mints = [mint] if allow_mint else []

    return mint, state_path, status_path


def test_unknown_lots_and_manual_override_separate(tmp_path, monkeypatch):
    mint, state_path, status_path = _make_state_with_override(tmp_path)

    # Unknown lots remain non-tradable by default: trading_bag_raw is zero.
    state = RuntimeState.model_validate_json(state_path.read_text())
    ms = state.mints[mint]
    assert int(ms.trading_bag_raw) == 0

    # Manual override is visible in dashboard payload
    payload = build_dashboard_payload(tmp_path)
    thb = payload["token_holdings_breakdown"][mint]
    assert thb["manual_override_raw"] == 400
    # transfer/unknown remainder is current_balance_raw - tx_derived_raw - manual_override_raw
    assert thb["current_balance_raw"] == 1000
    assert thb["tx_derived_raw"] == 0
    assert thb["unknown_or_transfer_raw"] == 600


def test_manual_override_does_not_change_reconciliation_sum_of_lots(tmp_path):
    mint, state_path, status_path = _make_state_with_override(tmp_path)
    state = RuntimeState.model_validate_json(state_path.read_text())
    from mint_ladder_bot.models import StatusFile

    status = StatusFile.model_validate_json(status_path.read_text())
    recs = compute_reconciliation_records(state, status, mint_filter=mint)
    assert len(recs) == 1
    r = recs[0]
    # sum_active_lots_raw remains based on lots only (no manual override included)
    assert r.sum_active_lots_raw == 0
    assert r.wallet_balance_raw == 1000

