#!/usr/bin/env python3
"""
Smoke test: detect → filter → (no live buy) → verify pipeline and config.

Run from mint-ladder-bot root:
  python scripts/smoke_sniper_pipeline.py

Sets SNIPER_TEST_MINTS temporarily, runs detect_all and filter_candidate,
verifies config sniper fields load. Does not send any transaction.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Load .env
_env = _ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and k not in os.environ:
                os.environ[k] = v

# Test mints for detection (WSOL + one other)
os.environ["SNIPER_TEST_MINTS"] = "So11111111111111111111111111111111111111112,TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

from mint_ladder_bot.config import Config
from mint_ladder_bot.sniper_engine import detect_all, filter_candidate
from mint_ladder_bot.lot_invariants import check_all_state_invariants
from mint_ladder_bot.models import RuntimeState
from datetime import datetime, timezone

def main():
    print("Smoke: sniper pipeline (detect → filter, no buy)")
    config = Config()
    print("  SNIPER_ENABLED:", getattr(config, "sniper_enabled", False))
    print("  SNIPER_BUY_SOL:", getattr(config, "sniper_buy_sol", 0))
    print("  SNIPER_MIN_SOL_RESERVE:", getattr(config, "sniper_min_sol_reserve", 0))

    candidates = detect_all(limit_per_source=5)
    print("  detect_all() candidates:", len(candidates))
    if not candidates:
        print("  WARN: no candidates (set SNIPER_TEST_MINTS or PUMPFUN_NEW_TOKENS_URL)")
    for c in candidates[:3]:
        fr = filter_candidate(c, require_metadata=False, min_liquidity_usd=0.0)
        print("    mint=%s source=%s filter=%s reason=%s" % (c.mint[:12], c.source, fr.passed, fr.reason))

    state = RuntimeState(version=1, started_at=datetime.now(timezone.utc), status_file="status.json", mints={})
    errors = check_all_state_invariants(state, None)
    print("  check_all_state_invariants (empty state):", len(errors), "errors")
    print("  OK smoke completed.")

if __name__ == "__main__":
    main()
