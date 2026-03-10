#!/usr/bin/env python3
"""
Production trigger table: for every token with runtime_tradable_raw > 0, output
token, current_price, entry, first_unexecuted_step_index, first_target_price,
distance_to_trigger_abs/pct, liquidity, blocked_reason, eligible_to_sell_now.

Uses state.json + status.json; optionally health_status.json for sell_readiness from runner.
Priority tokens: $HACHI, 1, WHM, WAR, 丙午.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Default ladder multiples (medium/static) for approximate target when runner sell_readiness not present
LADDER_MULTIPLES = [
    1.10, 1.20, 1.30, 1.40, 1.50, 1.65, 1.80, 2.00, 2.25, 2.50,
    3.00, 3.50, 4.00, 5.00, 6.00, 7.50, 10.0, 15.0, 20.0, 30.0,
]


def _first_unexecuted_step_index(executed_steps: dict) -> int | None:
    """First 1-based step index not in executed_steps. Keys may be '1','2' or '1.10','1.20'."""
    if not isinstance(executed_steps, dict):
        return 1
    for i in range(1, 21):
        if str(i) in executed_steps:
            continue
        legacy = f"{LADDER_MULTIPLES[i - 1]:.2f}"
        if legacy in executed_steps:
            continue
        return i
    return None


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", type=Path, default=None)
    ap.add_argument("--status", type=Path, default=None)
    ap.add_argument("--health", type=Path, default=None)
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    state_path = args.state or root / "state.json"
    status_path = args.status or root / "status.json"
    health_path = args.health or root / "health_status.json"
    if not state_path.exists():
        print("state.json not found", file=sys.stderr)
        return 1
    if not status_path.exists():
        print("status.json not found", file=sys.stderr)
        return 1

    state = json.loads(state_path.read_text())
    status = json.loads(status_path.read_text())
    health = None
    if health_path.exists():
        try:
            health = json.loads(health_path.read_text())
        except Exception:
            pass

    sell_readiness = (health or {}).get("sell_readiness") or {}
    status_by_mint = {m["mint"]: m for m in (status.get("mints") or []) if isinstance(m, dict) and m.get("mint")}
    state_mints = state.get("mints") or {}

    sys.path.insert(0, str(root))
    from mint_ladder_bot import dashboard_truth as dt

    priority_symbols = {"$HACHI", "1", "WHM", "WAR", "丙午"}
    rows = []

    for mint_id, ms in state_mints.items():
        if not isinstance(ms, dict):
            continue
        ss = status_by_mint.get(mint_id) or {}
        decimals = int(ss.get("decimals") or ms.get("decimals") or 6)
        symbol = ss.get("symbol") or ms.get("symbol") or mint_id[:8]
        truth = dt.token_truth(mint_id, ms, ss, decimals=decimals, symbol=symbol, sold_raw_from_steps=0)
        runtime_tradable_raw = truth.get("runtime_tradable_raw", 0) or 0
        if runtime_tradable_raw <= 0:
            continue

        entry_sol = truth.get("entry_sol_per_token")
        current_price = None
        liquidity_usd = truth.get("liquidity_usd")
        market = (ss.get("market") or {}).get("dexscreener") if isinstance(ss.get("market"), dict) else None
        if isinstance(market, dict) and market.get("price_native") is not None:
            try:
                current_price = float(market["price_native"])
            except (TypeError, ValueError):
                pass
        alerts = truth.get("alerts") or []
        blocked_reason = "; ".join(alerts) if alerts else ""

        # Prefer runner sell_readiness if present
        sr = sell_readiness.get(mint_id) or {}
        next_target_price = sr.get("next_target_price")
        next_step_index = sr.get("next_step_index")
        distance_pct = sr.get("distance_to_next_target_pct")
        sell_ready_now = sr.get("sell_ready_now", False)
        sell_blocked = sr.get("sell_blocked_reason") or blocked_reason

        if next_target_price is None and entry_sol is not None and entry_sol > 0:
            executed = ms.get("executed_steps") or {}
            first_idx = _first_unexecuted_step_index(executed)
            if first_idx is not None:
                next_step_index = first_idx
                next_target_price = entry_sol * LADDER_MULTIPLES[first_idx - 1]
                if current_price is not None and next_target_price and next_target_price > 0:
                    distance_pct = ((current_price - next_target_price) / next_target_price) * 100.0
                    sell_ready_now = current_price >= next_target_price and not sell_blocked

        distance_abs = None
        if current_price is not None and next_target_price is not None:
            distance_abs = current_price - next_target_price
        if distance_pct is None and current_price is not None and next_target_price and next_target_price > 0:
            distance_pct = ((current_price - next_target_price) / next_target_price) * 100.0

        why_no_trigger = ""
        if next_target_price is None:
            if entry_sol is None or entry_sol <= 0:
                why_no_trigger = "no_entry"
            else:
                why_no_trigger = "no_ladder_or_all_executed"

        rows.append({
            "token": symbol,
            "mint": mint_id[:16],
            "runtime_tradable_raw": runtime_tradable_raw,
            "current_price_sol_per_token": current_price,
            "entry_price_sol_per_token": entry_sol,
            "first_unexecuted_ladder_step_index": next_step_index,
            "first_unexecuted_ladder_target_price": next_target_price,
            "distance_to_trigger_abs": distance_abs,
            "distance_to_trigger_pct": distance_pct,
            "liquidity": liquidity_usd,
            "blocked_reason": sell_blocked or why_no_trigger or "-",
            "eligible_to_sell_now": sell_ready_now and not sell_blocked,
        })

    # Sort: priority first, then by distance_pct (closest to trigger first)
    def key(r):
        sym = r["token"]
        prio = 0 if sym in priority_symbols else 1
        dist = r.get("distance_to_trigger_pct")
        dist = dist if dist is not None else 999
        return (prio, -dist if dist != 999 else 999)

    rows.sort(key=key)

    print("Production trigger table (runtime_tradable_raw > 0)")
    print("=" * 130)
    print(f"{'token':<12} {'tradable_raw':>14} {'current':>10} {'entry':>10} {'step':>4} {'target':>10} {'dist_abs':>10} {'dist_pct':>8} {'liq':>10} {'blocked':<28} {'ready':<5}")
    print("-" * 130)
    for r in rows:
        cur = r["current_price_sol_per_token"]
        ent = r["entry_price_sol_per_token"]
        step = r["first_unexecuted_ladder_step_index"]
        tgt = r["first_unexecuted_ladder_target_price"]
        dabs = r["distance_to_trigger_abs"]
        dpct = r["distance_to_trigger_pct"]
        liq = r["liquidity"]
        blk = (r["blocked_reason"] or "-")[:26]
        ready = "yes" if r["eligible_to_sell_now"] else "no"
        cur_s = f"{cur:.2e}" if cur is not None else "N/A"
        ent_s = f"{ent:.2e}" if ent is not None else "N/A"
        step_s = str(step) if step is not None else "N/A"
        tgt_s = f"{tgt:.2e}" if tgt is not None else "N/A"
        dabs_s = f"{dabs:.2e}" if dabs is not None else "N/A"
        dpct_s = f"{dpct:+.1f}%" if dpct is not None else "N/A"
        liq_s = f"{liq:.0f}" if liq is not None else "N/A"
        print(f"{r['token']:<12} {r['runtime_tradable_raw']:>14} {cur_s:>10} {ent_s:>10} {step_s:>4} {tgt_s:>10} {dabs_s:>10} {dpct_s:>8} {liq_s:>10} {blk:<28} {ready:<5}")
    print("=" * 130)
    return 0


if __name__ == "__main__":
    sys.exit(main())
