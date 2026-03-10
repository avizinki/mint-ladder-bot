#!/usr/bin/env python3
"""
Print a short human-readable monitoring summary from state.json and run.log.

Read-only: wallet exposure, failures detected, cooldowns. No PnL.
Imports mint_ladder_bot.monitoring.runtime_monitor (run from mint-ladder-bot root).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

# Allow importing mint_ladder_bot when run as scripts/monitoring_summary.py from repo root.
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _ensure_path(p: str) -> Path:
    return Path(p).resolve()


def _run_monitor(state_path: Path, log_path: Path, status_path: Optional[Path]) -> dict:
    try:
        from mint_ladder_bot.monitoring.runtime_monitor import run_monitoring
    except ImportError as e:
        print("Error: run from mint-ladder-bot root so mint_ladder_bot is importable:", e, file=sys.stderr)
        sys.exit(1)
    return run_monitoring(state_path, log_path, status_path)


def _print_summary(summary: dict) -> None:
    w = summary.get("wallet_exposure") or {}
    for msg in summary.get("warnings") or []:
        print(f"Warning: {msg}")

    print("--- Wallet exposure (no PnL) ---")
    print(f"Wallet: {w.get('wallet_id', 'unknown')}")
    print(f"Mints: {w.get('mint_count', 0)}  |  Executed steps (total): {w.get('executed_steps_total', 0)}")
    print(f"Failures (total): {w.get('failure_count_total', 0)}  |  Mints paused: {w.get('mints_paused', 0)}  |  Cooldowns active: {w.get('cooldown_active_count', 0)}")
    print(f"Buybacks total SOL: {w.get('buybacks_total_sol', 0)}")
    if any(w.get(k) is not None for k in ("sells_ok", "sells_fail", "buybacks_ok", "buybacks_fail")):
        print(f"Session: sells_ok={w.get('sells_ok', 0)} sells_fail={w.get('sells_fail', 0)} buybacks_ok={w.get('buybacks_ok', 0)} buybacks_fail={w.get('buybacks_fail', 0)}")

    failures = summary.get("failures_and_abnormal") or []
    print("\n--- Failures / abnormal conditions ---")
    if not failures:
        print("None detected.")
    else:
        for ev in failures:
            cond = ev.get("condition", "?")
            parts = [f"  [{cond}]"]
            if ev.get("mint"):
                parts.append(f"mint={ev['mint']}")
            if ev.get("paused_until"):
                parts.append(f"paused_until={ev['paused_until']}")
            if ev.get("last_error"):
                parts.append(f"last_error={ev['last_error']}")
            if ev.get("step_id"):
                parts.append(f"step_id={ev['step_id']}")
            if ev.get("rpc_failures_count") is not None:
                parts.append(f"rpc_failures={ev['rpc_failures_count']}")
            print(" ".join(parts))

    cooldowns = summary.get("cooldowns") or {}
    print("\n--- Cooldowns ---")
    for m in cooldowns.get("per_mint_pause") or []:
        print(f"  Mint pause: {m.get('mint')} until {m.get('paused_until')}  ({m.get('last_error') or ''})")
    for m in cooldowns.get("per_mint_cooldown") or []:
        print(f"  Cooldown: {m.get('mint')} until {m.get('cooldown_until')}")
    global_until = cooldowns.get("global_trading_paused_until")
    if global_until:
        print(f"  Global RPC pause until: {global_until}")
    if not (cooldowns.get("per_mint_pause") or cooldowns.get("per_mint_cooldown") or global_until):
        print("  None active.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Print monitoring summary from state.json and run.log (no PnL).")
    ap.add_argument("state", type=_ensure_path, help="Path to state.json")
    ap.add_argument("log", type=_ensure_path, help="Path to run.log")
    ap.add_argument("--status", "-s", type=_ensure_path, default=None, help="Optional path to status.json")
    args = ap.parse_args()

    summary = _run_monitor(args.state, args.log, args.status)
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
