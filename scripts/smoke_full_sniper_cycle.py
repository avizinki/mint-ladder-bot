#!/usr/bin/env python3
"""
Smoke: full sniper cycle (detect → filter → simulate buy → confirm fill stub → create lot → check invariants).

Does not send real transactions. Uses SNIPER_TEST_MINTS, creates a lot in a temp state,
verifies no duplicate lots, and that invariants pass.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_env = _ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and k not in os.environ:
                os.environ[k] = v

# Ensure test mints for detection
if "SNIPER_TEST_MINTS" not in os.environ or not os.environ["SNIPER_TEST_MINTS"].strip():
    os.environ["SNIPER_TEST_MINTS"] = "So11111111111111111111111111111111111111112"

from mint_ladder_bot.sniper_engine import detect_all, filter_candidate, create_lot_in_state, persist_sniper_lot
from mint_ladder_bot.sniper_engine.launch_detector import LaunchCandidate
from mint_ladder_bot.lot_invariants import check_duplicate_lot_for_tx, check_lot_invariants, check_all_state_invariants
from mint_ladder_bot.models import RuntimeState
from mint_ladder_bot.state import load_state, save_state_atomic

STATUS_JSON = '''{"version":1,"created_at":"2026-01-01T00:00:00Z","wallet":"11111111111111111111111111111111","rpc":{"endpoint":"https://x.com"},"sol":{"lamports":0,"sol":0.0},"mints":[]}'''


def main():
    print("Smoke: full sniper cycle (simulated buy, no live tx)")
    candidates = detect_all(limit_per_source=5)
    if not candidates:
        print("  SKIP: no candidates (set SNIPER_TEST_MINTS)")
        return
    c = candidates[0]
    fr = filter_candidate(c, require_metadata=False, min_liquidity_usd=0.0)
    if not fr.passed:
        print("  FAIL: filter rejected", fr.reason)
        return
    print("  detect + filter OK")

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        status_path = Path(tmp) / "status.json"
        status_path.write_text(STATUS_JSON)
        state = load_state(state_path, status_path)

        # Simulate confirm_fill → token_raw, entry_price
        token_raw = 1_000_000
        entry_price = 1e-6
        tx_sig = "simulated_tx_sig_123"
        trading_bag_raw = int(token_raw * 0.2)
        moonbag_raw = 0

        if check_duplicate_lot_for_tx(state, tx_sig, c.mint, None):
            print("  FAIL: duplicate lot (unexpected)")
            return
        lot = create_lot_in_state(
            state, c.mint, token_raw, entry_price, tx_sig,
            str(trading_bag_raw), str(moonbag_raw), program_or_venue="jupiter",
        )
        persist_sniper_lot(state_path, status_path, state, journal_path=None, created_lot=lot, token_raw=token_raw, buy_sol=0.02)
        print("  lot created:", lot.lot_id[:8])

        if check_duplicate_lot_for_tx(state, tx_sig, c.mint, None):
            print("  duplicate check OK (second call returns True)")
        ms = state.mints.get(c.mint)
        assert ms is not None
        errs = check_lot_invariants(c.mint, ms, None)
        if errs:
            print("  FAIL: invariants", errs)
            return
        print("  invariants OK")
        all_errs = check_all_state_invariants(state, None)
        assert not all_errs
    print("  OK smoke full sniper cycle completed.")


if __name__ == "__main__":
    main()
