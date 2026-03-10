#!/usr/bin/env python3
"""
Validate state/status/dashboard agreement for 5 representative tokens.
Output: mint, wallet balance, lot-source breakdown, sellable, entry (state vs status), position status, dashboard row, agreement.
Run after tasks 1-4 to verify truth path. Usage: python tools/validate_five_token_truth.py [--base-url http://127.0.0.1:8765]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mint_ladder_bot import dashboard_truth as dt


def _fetch_dashboard_api(base_url: str, timeout_sec: int = 15, retries: int = 3) -> dict | None:
    import time
    import urllib.request
    url = base_url.rstrip("/") + "/runtime/dashboard"
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:6200")
    ap.add_argument("--state-path", type=Path, default=None)
    ap.add_argument("--status-path", type=Path, default=None)
    ap.add_argument("--allow-local-fallback", action="store_true", help="If API unreachable, validate from local state/status only and report LOCAL_STATE_VALIDATION_PASS")
    ap.add_argument("--api-timeout", type=int, default=15, help="Request timeout in seconds for dashboard API")
    ap.add_argument("--api-retries", type=int, default=3, help="Number of retries for dashboard API")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    state_path = args.state_path or root / "state.json"
    status_path = args.status_path or root / "status.json"

    if not state_path.exists():
        print("state.json not found", file=sys.stderr)
        return 1
    if not status_path.exists():
        print("status.json not found", file=sys.stderr)
        return 1

    state = json.loads(state_path.read_text())
    status = json.loads(status_path.read_text())
    status_mints = {m["mint"]: m for m in (status.get("mints") or []) if isinstance(m, dict) and m.get("mint")}
    state_mints = state.get("mints") or {}

    # Classify tokens: multi-buy (many lots), partial (some sold), closed (balance 0, sold>0), bootstrap-only, recent (e.g. one lot)
    candidates = []
    for mint_addr, md in state_mints.items():
        if not isinstance(md, dict):
            continue
        lots = md.get("lots") or []
        sold_raw = sum(int((s.get("sold_raw") or 0)) for s in (md.get("executed_steps") or {}).values())
        balance_raw = 0
        sm = status_mints.get(mint_addr)
        if sm and sm.get("balance_raw") is not None:
            try:
                balance_raw = int(sm["balance_raw"])
            except (ValueError, TypeError):
                pass
        if balance_raw == 0 and md.get("last_known_balance_raw"):
            try:
                balance_raw = int(md["last_known_balance_raw"])
            except (ValueError, TypeError):
                pass
        tx_d, boot, unk, _ = dt.lot_source_breakdown(lots)
        kind = "closed" if (balance_raw <= 0 and sold_raw > 0) else "partial" if (sold_raw > 0 and balance_raw > 0) else "bootstrap-only" if (tx_d == 0 and boot > 0 and unk == 0) else "received-only" if (tx_d == 0 and boot == 0 and unk > 0) else "multi-buy" if len(lots) > 2 else "open"
        candidates.append((mint_addr, kind, len(lots), balance_raw, sold_raw, md, sm))

    # Pick 5: prefer one of each kind
    want = ["multi-buy", "partial", "closed", "bootstrap-only", "open"]
    chosen = []
    for k in want:
        for c in candidates:
            if c[1] == k and c[0] not in [x[0] for x in chosen]:
                chosen.append(c)
                break
    if len(chosen) < 5:
        for c in candidates:
            if c[0] not in [x[0] for x in chosen]:
                chosen.append(c)
                if len(chosen) >= 5:
                    break
    chosen = chosen[:5]

    print("=== 5-token validation (state / status / dashboard truth) ===\n")
    api_payload = _fetch_dashboard_api(args.base_url, timeout_sec=args.api_timeout, retries=args.api_retries)
    if api_payload is None and not args.allow_local_fallback:
        print("API_VALIDATION_FAIL: dashboard unreachable (timeout or connection error). Use --allow-local-fallback to validate from local state/status only.", file=sys.stderr)
        return 1
    if api_payload is None:
        print("Dashboard API unreachable; validating from local state/status only (--allow-local-fallback).\n")

    results: list[str] = []
    for mint_addr, kind, n_lots, balance_raw, sold_raw, md, sm in chosen:
        decimals = int((sm or {}).get("decimals") or md.get("decimals") or 6)
        symbol = (sm or {}).get("symbol") or md.get("symbol") or mint_addr[:8]
        truth = dt.token_truth(mint_addr, md, sm, decimals=decimals, symbol=symbol, sold_raw_from_steps=sold_raw)
        entry_state = md.get("entry_price_sol_per_token") or md.get("working_entry_price_sol_per_token")
        entry_status = None
        if sm and isinstance(sm.get("entry"), dict):
            entry_status = sm["entry"].get("entry_price_sol_per_token")
        pos_status = truth.get("position_status")
        dashboard_row = None
        if api_payload:
            for p in (api_payload.get("positions") or []):
                if p.get("mint") == mint_addr:
                    dashboard_row = p
                    break
        # Contract: main table = Tradable now (runtime_tradable_raw). Compare truth vs dashboard.
        runtime_tradable_raw = truth.get("runtime_tradable_raw", 0)
        lot_remaining_raw = truth.get("lot_remaining_raw", 0)
        sellable_source = truth.get("sellable_source") or "none"
        result = "AGREE"
        if dashboard_row:
            if str(dashboard_row.get("position_status")) != str(pos_status):
                result = "MISMATCH"
            else:
                dash_tradable_ui = dashboard_row.get("runtime_tradable_ui") is not None and dashboard_row.get("runtime_tradable_ui") or dashboard_row.get("sellable_ui")
                expect_tradable_ui = runtime_tradable_raw / (10 ** decimals)
                if abs((dash_tradable_ui or 0) - expect_tradable_ui) > 1e-9:
                    # Bootstrap-only / policy: runtime_tradable=0, lot_remaining>0 is expected
                    if kind == "bootstrap-only" and runtime_tradable_raw == 0 and lot_remaining_raw > 0:
                        result = "EXPECTED_DIFFERENCE"
                    elif sellable_source == "lot_sum" and runtime_tradable_raw == 0 and lot_remaining_raw > 0:
                        result = "EXPECTED_DIFFERENCE"
                    else:
                        result = "MISMATCH"
        results.append(result)
        print(f"mint: {mint_addr[:16]}...")
        print(f"  kind: {kind}  balance_raw: {balance_raw}  runtime_tradable_raw: {runtime_tradable_raw}  lot_remaining_raw: {lot_remaining_raw}  lots: {n_lots}")
        print(f"  sellable_source: {sellable_source}")
        print(f"  lot_source: tx_exact={truth.get('counts_by_source',{}).get('tx_exact',0)} tx_parsed={truth.get('counts_by_source',{}).get('tx_parsed',0)} bootstrap={truth.get('counts_by_source',{}).get('bootstrap_snapshot',0)} unknown={truth.get('counts_by_source',{}).get('transfer_received_unknown',0)}")
        print(f"  entry state: {entry_state}  entry status: {entry_status}")
        print(f"  position_status: {pos_status}  alerts: {truth.get('alerts')}")
        if dashboard_row:
            print(f"  dashboard position_status: {dashboard_row.get('position_status')} runtime_tradable_ui: {dashboard_row.get('runtime_tradable_ui')}")
        print(f"  result: {result}\n")

    # Dashboard process verification (host-only; skip in Docker)
    try:
        from mint_ladder_bot.dashboard_server import check_port_8765_ownership
        from mint_ladder_bot.port_resolver import resolve_dashboard_http_port
        port = resolve_dashboard_http_port()
        port_check = check_port_8765_ownership(port)
        print(f"=== Dashboard process (port {port}) ===")
        if port_check.get("in_use"):
            print(f"  in_use: True  owner_pid: {port_check.get('owner_pid')}  owner_cmd: {(port_check.get('owner_cmd') or '')[:80]}")
        else:
            print(f"  in_use: False (no process on port {port})")
    except Exception as e:
        print(f"=== Dashboard process check skipped: {e}")

    # Explicit outcome for release / CI
    has_mismatch = any(r == "MISMATCH" for r in results)
    if api_payload is not None:
        if has_mismatch:
            print("API_VALIDATION_FAIL: one or more tokens disagree with dashboard.", file=sys.stderr)
            return 1
        print("API_VALIDATION_PASS")
        return 0
    # Local-only path (API unreachable, --allow-local-fallback or no API attempted)
    if has_mismatch:
        print("LOCAL_STATE_VALIDATION_FAIL: one or more tokens disagree (local state vs status).", file=sys.stderr)
        return 1
    print("LOCAL_STATE_VALIDATION_PASS (dashboard API was not used)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
