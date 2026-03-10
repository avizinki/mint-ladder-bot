from datetime import datetime, timezone

from mint_ladder_bot.dashboard_server import build_dashboard_payload
from mint_ladder_bot.models import RuntimeState, SolBalance, StatusFile, RpcInfo


def test_dashboard_prefers_state_sol_over_status_sol(tmp_path):
    # State has live SOL (0.31), status has older snapshot (0.30).
    state = RuntimeState(
        version=1,
        started_at=datetime.now(tz=timezone.utc),
        status_file=str(tmp_path / "status.json"),
        wallet="WalletSolPreferenceTest",
        sol=SolBalance(lamports=310_000_000, sol=0.31),
        mints={},
    )
    (tmp_path / "state.json").write_text(state.model_dump_json())

    status = StatusFile(
        version=1,
        created_at=datetime.now(tz=timezone.utc),
        wallet=state.wallet,
        rpc=RpcInfo(endpoint="http://example"),
        sol=SolBalance(lamports=300_000_000, sol=0.3),
        mints=[],
    )
    (tmp_path / "status.json").write_text(status.model_dump_json())

    # Minimal health/alerts files so dashboard loader succeeds.
    (tmp_path / "health_status.json").write_text('{"ok": true}')
    (tmp_path / "uptime_alerts.jsonl").write_text("")

    payload = build_dashboard_payload(tmp_path)
    wallet = payload.get("wallet") or {}
    sol_block = wallet.get("sol")
    assert isinstance(sol_block, dict)
    # Must reflect live state.sol, not stale status.sol.
    assert sol_block.get("sol") == 0.31
    assert sol_block.get("lamports") == 310_000_000

