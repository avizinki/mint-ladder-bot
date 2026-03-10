from __future__ import annotations

import json
from pathlib import Path

from mint_ladder_bot import health as health_mod


def test_health_status_written_atomically(monkeypatch, tmp_path):
    # Point health_status path to a temp location.
    health_path = tmp_path / "health_status.json"

    def _fake_get_health_status_path():
        return health_path

    monkeypatch.setattr(health_mod, "get_health_status_path", _fake_get_health_status_path)

    class _State:
        wallet = "WALLET"

    # Write health status.
    health_mod.write_health_status(data_dir=tmp_path, state=_State(), runtime_info={"cycles": 1})

    assert health_path.exists()
    data = json.loads(health_path.read_text(encoding="utf-8"))
    assert data["wallet"] == "WALLET"
    assert data["cycles"] == 1
    # Ensure temp file is not left behind.
    assert not health_path.with_suffix(".json.tmp").exists()

