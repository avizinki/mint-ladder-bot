"""
Lightweight HTTP server for the runtime dashboard API.

Serves GET / (health) and GET /runtime/dashboard (aggregated JSON from
health_status.json, status.json, state.json, uptime_alerts.jsonl).
Production-hardened: read retries, no crash on missing/partial files,
response timestamp, optional cycle counter, request latency logging.
Runs in a daemon thread so it does not block the runtime.
No secrets in output.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Read retry for files that may be mid-write by runtime
_READ_RETRIES = 3
_READ_RETRY_DELAY_SEC = 0.05

# Response cache: serve same payload for this many seconds to keep response time < 200ms. Use 0 to always refresh (sell/balance display current).
_CACHE_TTL_SEC = 0.0
_cache_lock = threading.Lock()
_cached_payload: Optional[Dict[str, Any]] = None
_cached_at: float = 0.0
_cached_data_dir: Optional[Path] = None


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    """Read JSON file with retries. Returns None on missing, partial, or invalid data."""
    if not path.exists():
        return None
    last_err: Optional[Exception] = None
    for attempt in range(_READ_RETRIES):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            last_err = e
            if attempt < _READ_RETRIES - 1:
                time.sleep(_READ_RETRY_DELAY_SEC)
    if last_err:
        logger.debug("Read %s failed after %d attempts: %s", path, _READ_RETRIES, last_err)
    return None


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read JSONL with retries. Returns [] on failure or partial read."""
    if not path.exists():
        return []
    last_err: Optional[Exception] = None
    for attempt in range(_READ_RETRIES):
        try:
            text = path.read_text(encoding="utf-8")
            result: List[Dict[str, Any]] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return result
        except OSError as e:
            last_err = e
            if attempt < _READ_RETRIES - 1:
                time.sleep(_READ_RETRY_DELAY_SEC)
    if last_err:
        logger.debug("Read JSONL %s failed after %d attempts: %s", path, _READ_RETRIES, last_err)
    return []


# Entry price and lot/position truth: single source in dashboard_truth (no duplicate logic).
from . import dashboard_truth as dt
from . import symbol_cache as sc
from .runtime_paths import get_run_log_path
from .strategy import LADDER_MULTIPLES

# Canonical ladder size: only steps 1..LADDER_TOTAL_LEVELS count toward executed_levels (excludes ext_* backfill keys).
LADDER_TOTAL_LEVELS = len(LADDER_MULTIPLES)


def _last_cycle_from_log(log_path: Path) -> Optional[int]:
    """Parse run.log for last 'Cycle N summary' line. Returns cycle number or None."""
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    # Last occurrence wins
    match = None
    for line in reversed(text.splitlines()):
        m = re.search(r"Cycle\s+(\d+)\s+summary", line)
        if m:
            match = m
            break
    return int(match.group(1)) if match else None


def _build_discovery_section(state: Dict[str, Any] | None) -> Dict[str, Any]:
    """
    Build the top-level discovery section for the dashboard payload.
    Read-only: derived purely from state fields written by DiscoveryPipeline.
    """
    s = state or {}
    disc_stats = s.get("discovery_stats") or {}
    recent = s.get("discovery_recent_candidates") or []
    rejected = s.get("discovery_rejected_candidates") or []

    ds: Dict[str, Any] = disc_stats if isinstance(disc_stats, dict) else {}

    # review_only mode flag: read from env so operators can see gating mode at a glance.
    review_only: bool = os.getenv("DISCOVERY_REVIEW_ONLY", "true").strip().lower() not in ("0", "false", "no")

    # Last 10 recent (accepted/enqueued) for display — full mint address for operator review/enqueue.
    recent_display: List[Dict[str, Any]] = []
    if isinstance(recent, list):
        for rec in recent[-10:]:
            if not isinstance(rec, dict):
                continue
            # Safe extraction of discovery_signals — truncate trigger_wallet for display
            signals = rec.get("discovery_signals") or {}
            signals_display: Dict[str, Any] = {}
            if isinstance(signals, dict):
                tw = signals.get("trigger_wallet")
                if tw:
                    signals_display["trigger_wallet"] = str(tw)[:12] + "..."
                for k in ("wallet_label", "buy_amount_sol", "signal_type", "price_change_pct_5m"):
                    if k in signals:
                        signals_display[k] = signals[k]
            recent_display.append({
                "mint": rec.get("mint", ""),
                "source_id": rec.get("source_id"),
                "symbol": rec.get("symbol"),
                "score": rec.get("score"),
                "score_breakdown": rec.get("score_breakdown") or {},
                "discovery_signals": signals_display,
                "outcome": rec.get("outcome"),
                "approval_path": rec.get("approval_path"),
                "enqueue_source": rec.get("enqueue_source"),
                "liquidity_usd": rec.get("liquidity_usd"),
                "discovered_at": rec.get("discovered_at"),
            })

    # Last 10 rejected with reasons — full mint address for debugging.
    rejected_display: List[Dict[str, Any]] = []
    if isinstance(rejected, list):
        for rec in rejected[-10:]:
            if not isinstance(rec, dict):
                continue
            rejected_display.append({
                "mint": rec.get("mint", ""),
                "source_id": rec.get("source_id"),
                "symbol": rec.get("symbol"),
                "rejection_reason": rec.get("rejection_reason"),
                "score": rec.get("score"),
                "score_breakdown": rec.get("score_breakdown") or {},
                "discovered_at": rec.get("discovered_at"),
            })

    # Per-source sub-stats (v2): {source_id: {discovered, accepted, rejected, enqueued}}
    source_stats: Dict[str, Any] = ds.get("source_stats") or {}

    # Enrichment stats
    enrichment_stats: Dict[str, Any] = {
        "checks_run": ds.get("enrichment_checks_run", 0),
        "partial_count": ds.get("enrichment_partial_count", 0),
        "hard_reject_count": ds.get("enrichment_hard_reject_count", 0),
    }

    return {
        "review_only": review_only,
        "total_discovered": ds.get("total_discovered", 0),
        "total_accepted": ds.get("total_accepted", 0),
        "total_rejected": ds.get("total_rejected", 0),
        "total_enqueued": ds.get("total_enqueued", 0),
        "source_breakdown": ds.get("by_source") or {},
        "source_stats": source_stats,
        "rejection_reason_breakdown": ds.get("by_rejection_reason") or {},
        "recent_accepted_count": len(recent) if isinstance(recent, list) else 0,
        "recent_rejected_count": len(rejected) if isinstance(rejected, list) else 0,
        "recent_candidates": recent_display,
        "recent_rejected": rejected_display,
        "enrichment_stats": enrichment_stats,
    }


def _build_sniper_summary(state: Dict[str, Any] | None) -> Dict[str, Any]:
    sniper_state = state or {}
    sniper_stats = sniper_state.get("sniper_stats") or {}
    recent_hour = sniper_state.get("sniper_recent_success_timestamps_hour") or []
    recent_day = sniper_state.get("sniper_recent_success_timestamps_day") or []
    last_decisions = sniper_state.get("sniper_last_decisions") or []
    pending_attempts = sniper_state.get("sniper_pending_attempts") or {}
    manual_queue = sniper_state.get("sniper_manual_seed_queue") or []

    last_decision_at = None
    if isinstance(last_decisions, list):
        try:
            last_decision_at = max(
                d.get("ts")
                for d in last_decisions
                if isinstance(d, dict) and "ts" in d
            )
        except Exception:
            last_decision_at = None

    last_buy_at = sniper_stats.get("last_buy_at") if isinstance(sniper_stats, dict) else None
    open_positions_count = sniper_stats.get("open_sniper_positions_count", 0) if isinstance(sniper_stats, dict) else 0

    return {
        "enabled": False,
        "mode": "disabled",
        "discovery_enabled": False,
        "manual_seed_queue_size": len(manual_queue) if isinstance(manual_queue, list) else 0,
        "pending_attempts_count": len(pending_attempts) if isinstance(pending_attempts, dict) else 0,
        "open_sniper_positions_count": open_positions_count,
        "recent_success_count_1h": len(recent_hour) if isinstance(recent_hour, list) else 0,
        "recent_success_count_24h": len(recent_day) if isinstance(recent_day, list) else 0,
        "last_decision_at": last_decision_at,
        "last_buy_at": last_buy_at,
    }


def build_dashboard_payload(data_dir: Path) -> Dict[str, Any]:
    """Build the /runtime/dashboard response from files in data_dir. Never raises."""
    data_dir = data_dir.resolve()
    health_path = data_dir / "health_status.json"
    status_path = data_dir / "status.json"
    state_path = data_dir / "state.json"
    alerts_path = data_dir / "uptime_alerts.jsonl"
    # Canonical run.log is in centralized runtime logs dir; fallback to data_dir/run.log for legacy files.
    from .runtime_paths import get_run_log_path
    log_path = get_run_log_path()

    try:
        runtime = _read_json(health_path)
        if runtime is None:
            runtime = {"ok": False, "error": "health_status.json not found or unreadable"}
        elif not isinstance(runtime, dict):
            runtime = {"ok": False, "error": "health_status invalid shape"}
    except Exception as e:
        logger.warning("Dashboard runtime read failed: %s", e)
        runtime = {"ok": False, "error": str(e)}

    try:
        status = _read_json(status_path)
        state = _read_json(state_path)
        alerts_raw = _read_jsonl(alerts_path)
        alerts = list(reversed(alerts_raw))
    except Exception as e:
        logger.warning("Dashboard read failed: %s", e)
        status = state = None
        alerts = []

    wallet: Dict[str, Any] = {}
    # Prefer live runtime truth from state.json; fall back to status.json only
    # when state does not yet have the corresponding fields (first boot).
    if state and isinstance(state, dict):
        if "wallet" in state:
            wallet["wallet"] = state["wallet"]
        if "sol" in state:
            wallet["sol"] = state["sol"]
    if status and isinstance(status, dict):
        if "wallet" not in wallet and "wallet" in status:
            wallet["wallet"] = status["wallet"]
        if "sol" not in wallet and "sol" in status:
            wallet["sol"] = status["sol"]

    # Build per-mint lookup from status (symbol, decimals, market/price); keep first per mint to avoid duplicates.
    tokens_by_mint: Dict[str, Dict[str, Any]] = {}
    status_mints = (status or {}).get("mints") if isinstance(status, dict) else None
    state_mints = (state or {}).get("mints") if isinstance(state, dict) else {}
    if not isinstance(state_mints, dict):
        state_mints = {}
    if isinstance(status_mints, list):
        for sm in status_mints:
            if not isinstance(sm, dict):
                continue
            mint_addr = sm.get("mint")
            if not mint_addr or mint_addr in tokens_by_mint:
                continue
            entry = dict(sm)
            ms = state_mints.get(mint_addr)
            ep = None
            if isinstance(ms, dict) and "entry_price_sol_per_token" in ms:
                ep = ms.get("entry_price_sol_per_token")
            if ep is None and isinstance(entry.get("entry"), dict):
                ep = entry["entry"].get("entry_price_sol_per_token")
            if ep is not None and (isinstance(ep, (int, float)) and (ep == 0 or ep == 0.0)):
                ep = None
            entry.setdefault("entry", {})
            entry["entry"] = {**(entry.get("entry") or {}), "entry_price_sol_per_token": ep}
            # Coerce zero current price to null so clients show N/A
            if isinstance(entry.get("market"), dict) and isinstance(entry["market"].get("dexscreener"), dict):
                pn = entry["market"]["dexscreener"].get("price_native")
                if pn is not None and (pn == 0 or pn == 0.0):
                    entry["market"]["dexscreener"] = {**entry["market"]["dexscreener"], "price_native": None}
            tokens_by_mint[mint_addr] = entry
    tokens = list(tokens_by_mint.values())

    decimals_by_mint = {m.get("mint"): m.get("decimals") for m in (status_mints or []) if isinstance(m, dict) and m.get("mint")}
    symbol_by_mint = {m.get("mint"): m.get("symbol") for m in (status_mints or []) if isinstance(m, dict) and m.get("mint")}
    pending_lots_count = dt.pending_lots_count_from_state(state) if state and isinstance(state, dict) else 0
    recent_buys: List[Dict[str, Any]] = []
    token_holdings_breakdown: Dict[str, Dict[str, Any]] = {}
    if state and isinstance(state, dict) and "mints" in state and isinstance(state["mints"], dict):
        for mint_addr, mint_data in state["mints"].items():
            if not isinstance(mint_data, dict):
                continue
            lots = mint_data.get("lots") or []
            tx_derived_raw, bootstrap_raw, transfer_unknown_raw, _ = dt.lot_source_breakdown(lots)
            sold_raw = 0
            executed_steps_list: List[Dict[str, Any]] = []
            for step_key, step_info in (mint_data.get("executed_steps") or {}).items():
                if isinstance(step_info, dict):
                    try:
                        sold_raw += int(step_info.get("sold_raw") or 0)
                        executed_steps_list.append({
                            "step_id": step_key,
                            "sig": step_info.get("sig"),
                            "time": step_info.get("time"),
                            "sold_raw": step_info.get("sold_raw"),
                            "sol_out": step_info.get("sol_out"),
                        })
                    except (ValueError, TypeError):
                        pass
            for lot in lots:
                row = dt.lot_display_row(lot, mint_addr, symbol=symbol_by_mint.get(mint_addr), decimals=decimals_by_mint.get(mint_addr))
                if row:
                    recent_buys.append(row)
            dec = decimals_by_mint.get(mint_addr, 6)
            try:
                current_balance_raw = int(mint_data.get("last_known_balance_raw") or 0)
            except (ValueError, TypeError):
                current_balance_raw = tx_derived_raw + bootstrap_raw + transfer_unknown_raw
            # Manual override inventory: explicit, operator-approved non-tx-proven holdings.
            manual_override_list = mint_data.get("manual_override_inventory") or []
            manual_override_raw = 0
            try:
                for rec in manual_override_list:
                    if not isinstance(rec, dict):
                        continue
                    manual_override_raw += int(rec.get("amount_raw") or 0)
            except (ValueError, TypeError):
                manual_override_raw = 0
            # Unknown / non-proven remainder from current balance after tx-derived + manual override.
            unknown_or_transfer_raw = current_balance_raw - tx_derived_raw - manual_override_raw
            if unknown_or_transfer_raw < 0:
                unknown_or_transfer_raw = 0
            token_holdings_breakdown[mint_addr] = {
                "symbol": symbol_by_mint.get(mint_addr),
                "decimals": dec,
                "current_balance_raw": current_balance_raw,
                "tx_derived_raw": tx_derived_raw,
                "bootstrap_snapshot_raw": bootstrap_raw,
                "transfer_unknown_raw": transfer_unknown_raw,
                "manual_override_raw": manual_override_raw,
                "unknown_or_transfer_raw": unknown_or_transfer_raw,
                "sold_raw": sold_raw,
                "sum_active_lots_raw": tx_derived_raw + bootstrap_raw + transfer_unknown_raw,
                "executed_steps": executed_steps_list,
            }
    recent_buys.sort(key=lambda x: (x.get("detected_at") or ""), reverse=True)
    recent_buys = recent_buys[:100]

    cycle_index: Optional[int] = None
    try:
        cycle_index = _last_cycle_from_log(log_path)
    except Exception:
        pass

    generated_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

    # CEO directive §8: wallet PnL, realized profit, open positions, liquidity health, error alerts
    # Derive current_sol from state: prefer state["sol"]["sol"], else state["sol"] if numeric (legacy).
    current_sol: Optional[float] = None
    if state and isinstance(state, dict):
        sol_val = state.get("sol")
        if isinstance(sol_val, dict) and "sol" in sol_val:
            try:
                current_sol = float(sol_val["sol"])
            except (TypeError, ValueError):
                pass
        elif isinstance(sol_val, (int, float)):
            current_sol = float(sol_val)
    session_start_sol: Optional[float] = None
    if state and isinstance(state, dict):
        ss = state.get("session_start_sol")
        if ss is not None:
            try:
                session_start_sol = float(ss)
            except (TypeError, ValueError):
                pass
    # Backfill session_start_sol from current_sol when missing so we show 0.0 PnL instead of N/A.
    if session_start_sol is None and current_sol is not None:
        session_start_sol = current_sol
    realized_sol = 0.0
    if state and isinstance(state, dict) and isinstance(state.get("mints"), dict):
        for mint_data in state["mints"].values():
            if not isinstance(mint_data, dict):
                continue
            for step_info in (mint_data.get("executed_steps") or {}).values():
                if isinstance(step_info, dict):
                    realized_sol += float(step_info.get("sol_out") or 0)
    wallet_pnl: Optional[float] = None
    if session_start_sol is not None and current_sol is not None:
        wallet_pnl = current_sol - session_start_sol
    # Fallback: derive current_sol from wallet (already filled from state/status) so PnL shows 0.0 instead of N/A.
    if wallet_pnl is None and isinstance(wallet.get("sol"), dict) and "sol" in wallet["sol"]:
        try:
            current_sol = float(wallet["sol"]["sol"])
            wallet_pnl = 0.0
        except (TypeError, ValueError):
            pass
    elif wallet_pnl is None and isinstance(wallet.get("sol"), (int, float)):
        wallet_pnl = 0.0
    # Open positions: tokens with a valid entry price (top-level or entry.entry_price_sol_per_token).
    def _has_entry_price(t: Dict[str, Any]) -> bool:
        ep = t.get("entry_price_sol_per_token")
        if ep is not None and isinstance(ep, (int, float)) and ep > 0:
            return True
        ent = t.get("entry") if isinstance(t.get("entry"), dict) else None
        ep = ent.get("entry_price_sol_per_token") if ent else None
        return ep is not None and isinstance(ep, (int, float)) and ep > 0
    open_positions = sum(1 for t in tokens if _has_entry_price(t))
    liquidity_health = "ok"
    for t in tokens:
        liq = None
        if isinstance(t.get("market"), dict) and isinstance(t["market"].get("dexscreener"), dict):
            liq = t["market"]["dexscreener"].get("liquidity_usd")
        if liq is not None and liq < 10_000:
            liquidity_health = "low"
            break
    error_alerts = [a for a in alerts if isinstance(a, dict) and (a.get("alert_type") == "error" or "error" in str(a.get("message", "")).lower())]

    # Never send None for numeric PnL/positions so frontend shows 0 instead of N/A (observability: 0 is a value).
    # Manual override configuration summary (for dashboard banner)
    from .config import Config as _Config  # local import to avoid import cycles at module import time
    _cfg = _Config()
    manual_override_summary = {
        "enabled": bool(getattr(_cfg, "enable_manual_override_inventory", False)),
        "allowed_mints": list(getattr(_cfg, "manual_override_allowed_mints", []) or []),
        "require_reason": bool(getattr(_cfg, "manual_override_require_reason", True)),
        "bypass_enabled": bool(getattr(_cfg, "manual_override_bypass_enabled", False)),
        "bypass_allowed_mints": list(getattr(_cfg, "manual_override_bypass_allowed_mints", []) or []),
        "bypass_min_override_raw": int(getattr(_cfg, "manual_override_bypass_min_override_raw", 0) or 0),
    }

    sniper_summary = _build_sniper_summary(state if isinstance(state, dict) else None)

    payload: Dict[str, Any] = {
        "runtime": runtime,
        "wallet": wallet,
        "tokens": tokens,
        "recent_buys": recent_buys,
        "lots_and_holdings": recent_buys,
        "lots_and_holdings_label": "Holdings by source (tx_derived = real swaps; bootstrap = migration/snapshot; transfer_unknown = receive not from swap). Not all rows are buys.",
        "token_holdings_breakdown": token_holdings_breakdown,
        "manual_override": manual_override_summary,
        "pending_lots_count": pending_lots_count,
        "alerts": alerts,
        "generated_at": generated_at,
        "wallet_pnl_sol": wallet_pnl if wallet_pnl is not None else 0.0,
        "realized_profit_sol": realized_sol,
        "open_positions_count": open_positions,
        "liquidity_health": liquidity_health,
        "error_alerts": error_alerts,
        "sniper_summary": sniper_summary,
        "sniper_pending_attempts": [],
        "sniper_recent_decisions": [],
        "discovery": _build_discovery_section(state if isinstance(state, dict) else None),
    }
    if cycle_index is not None:
        payload["cycle_index"] = cycle_index

    return payload


def _file_mtime_iso(path: Path) -> Optional[str]:
    """Return mtime as ISO string or None."""
    if not path.exists():
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def build_operator_dashboard_payload(data_dir: Path) -> Dict[str, Any]:
    """
    Build operator dashboard: one screen, unified positions table, expandable detail, events feed, runtime health.
    Never raises. Unknown => N/A; no fake zeros.
    """
    data_dir = data_dir.resolve()
    health_path = data_dir / "health_status.json"
    status_path = data_dir / "status.json"
    state_path = data_dir / "state.json"
    alerts_path = data_dir / "uptime_alerts.jsonl"
    events_path = data_dir / "events.jsonl"
    log_path = data_dir / "run.log"

    runtime = _read_json(health_path)
    if runtime is None or not isinstance(runtime, dict):
        runtime = {"ok": False, "error": "health_status.json not found or unreadable"}
    sell_readiness_by_mint = (runtime.get("sell_readiness") or {}) if isinstance(runtime, dict) else {}
    status = _read_json(status_path)
    state = _read_json(state_path)
    alerts = list(reversed(_read_jsonl(alerts_path)))
    events_raw = _read_jsonl(events_path)

    state_mints = (state or {}).get("mints") if isinstance(state, dict) else {}
    if not isinstance(state_mints, dict):
        state_mints = {}
    status_mints_list = (status or {}).get("mints") if isinstance(status, dict) else []
    if not isinstance(status_mints_list, list):
        status_mints_list = []

    # All mints = union of status and state
    all_mint_ids: set = set()
    for sm in status_mints_list:
        if isinstance(sm, dict) and sm.get("mint"):
            all_mint_ids.add(sm["mint"])
    for mint_id in state_mints:
        all_mint_ids.add(mint_id)

    status_by_mint = {}
    for sm in status_mints_list:
        if not isinstance(sm, dict) or not sm.get("mint"):
            continue
        status_by_mint[sm["mint"]] = sm

    symbol_cache = sc.load_symbol_cache(data_dir)
    symbol_by_mint: Dict[str, str] = {}
    for mid in all_mint_ids:
        ms = state_mints.get(mid) or {}
        ss = status_by_mint.get(mid) or {}
        sym = sc.resolve_symbol(mid, ms if isinstance(ms, dict) else None, ss if isinstance(ss, dict) else None, symbol_cache)
        symbol_by_mint[mid] = sym
        sc.ensure_symbol_cached(data_dir, mid, sym)

    # Derive SOL/USD from any non-stable token that has both price_usd and price_native.
    stable_symbols = {"USDC", "USDT", "USDS", "DAI", "USDH", "UXD", "PYUSD", "USDC.e"}
    sol_price_usd: Optional[float] = None
    for sm in status_mints_list:
        if not isinstance(sm, dict):
            continue
        sym = sm.get("symbol")
        if not sym or sym in stable_symbols:
            continue
        market = sm.get("market") or {}
        ds = market.get("dexscreener") if isinstance(market, dict) else None
        if not isinstance(ds, dict):
            continue
        # _safe_float is defined below; use direct float conversion here to avoid forward reference issues.
        try:
            pu = float(ds.get("price_usd") or 0)
            pn = float(ds.get("price_native") or 0)
        except (TypeError, ValueError):
            continue
        if pu > 0 and pn > 0:
            # price_usd = token_price_in_SOL * SOL_USD  =>  SOL_USD = price_usd / price_native
            sol_price_usd = pu / pn
            break

    def _safe_float(x: Any) -> Optional[float]:
        if x is None:
            return None
        try:
            v = float(x)
            return v if v == v else None
        except (TypeError, ValueError):
            return None

    generated_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    cycle_index = _last_cycle_from_log(log_path) if log_path.exists() else None

    # ---- HEADER ----
    wallet_full = (state or {}).get("wallet") if isinstance(state, dict) else (status or {}).get("wallet") if isinstance(status, dict) else None
    wallet_short = (str(wallet_full)[:6] + "…" + str(wallet_full)[-4:]) if wallet_full and len(str(wallet_full)) > 12 else (wallet_full or "N/A")
    sol_info = (state or {}).get("sol") if isinstance(state, dict) else (status or {}).get("sol") if isinstance(status, dict) else None
    current_sol = None
    if isinstance(sol_info, dict) and "sol" in sol_info:
        current_sol = _safe_float(sol_info.get("sol"))
    elif isinstance(sol_info, (int, float)):
        current_sol = float(sol_info)
    session_start = _safe_float((state or {}).get("session_start_sol")) if isinstance(state, dict) else None
    realized_total = 0.0
    if state and isinstance(state.get("mints"), dict):
        for mint_data in state["mints"].values():
            if not isinstance(mint_data, dict):
                continue
            for step_info in (mint_data.get("executed_steps") or {}).values():
                if isinstance(step_info, dict):
                    realized_total += float(step_info.get("sol_out") or 0)
    wallet_pnl = (current_sol - session_start) if (current_sol is not None and session_start is not None) else None
    error_alerts = [a for a in alerts if isinstance(a, dict) and (a.get("alert_type") == "error" or "error" in str(a.get("message", "")).lower())]
    runtime_ok = runtime.get("ok") is True
    runtime_status = "running" if runtime_ok else "degraded"
    runner_mode = runtime.get("runner_mode")
    trading_disabled = runtime.get("trading_disabled")
    swap_provider = runtime.get("swap_provider") or "Jupiter"
    header = {
        "title": "Mint Ladder — Runtime Dashboard",
        "wallet_short": wallet_short,
        "wallet_full": wallet_full,
        "mode": runner_mode if runner_mode else ("live" if runtime_ok else "degraded"),
        "runtime_status": runtime_status,
        "runner_mode": runner_mode,
        "trading_disabled": trading_disabled if trading_disabled is not None else False,
        "swap_provider": swap_provider,
        "last_refresh": generated_at,
        "refresh_interval_ms": 15000,
        "rpc_latency_ms": runtime.get("rpc_latency_ms"),
        "cycles": runtime.get("cycles"),
        "alerts_count": len(error_alerts),
        "paused_mints": runtime.get("paused_mints", 0),
    }

    # ---- KPIS ----
    open_count = 0
    closed_count = 0
    partial_count = 0
    total_active_lots = 0
    unrealized_total = 0.0
    for mint_id in all_mint_ids:
        ms = state_mints.get(mint_id)
        ss = status_by_mint.get(mint_id)
        balance_raw = 0
        if isinstance(ss, dict) and ss.get("balance_raw") is not None:
            try:
                balance_raw = int(ss["balance_raw"])
            except (ValueError, TypeError):
                pass
        if not balance_raw and isinstance(ms, dict):
            try:
                balance_raw = int(ms.get("last_known_balance_raw") or 0)
            except (ValueError, TypeError):
                pass
        sold_raw = 0
        if isinstance(ms, dict) and isinstance(ms.get("executed_steps"), dict):
            for step in (ms.get("executed_steps") or {}).values():
                if isinstance(step, dict):
                    sold_raw += int(step.get("sold_raw") or 0)
        lots = (ms or {}).get("lots") or [] if isinstance(ms, dict) else []
        active_lots = sum(1 for lot in lots if isinstance(lot, dict) and lot.get("status") != "duplicate_explained")
        total_active_lots += active_lots
        if balance_raw <= 0 and sold_raw > 0:
            closed_count += 1
        elif sold_raw > 0 and balance_raw > 0:
            partial_count += 1
        elif balance_raw > 0:
            open_count += 1
    kpis = {
        "wallet_sol": current_sol,
        "total_usd": None,
        "realized_pnl_sol": realized_total if realized_total != 0 else None,
        "unrealized_pnl_sol": None,
        "total_pnl_sol": wallet_pnl,
        "open_positions_count": open_count,
        "closed_positions_count": closed_count,
        "partial_positions_count": partial_count,
        "active_lots_count": total_active_lots,
        "total_tokens_count": len(all_mint_ids),
        "sells_24h": None,
        "buybacks_24h": None,
        "liquidity_risk_count": None,
        "cooldown_blocked_count": sum(1 for m in state_mints.values() if isinstance(m, dict) and m.get("cooldown_until")),
    }

    # ---- POSITIONS (one row per token) + POSITION_DETAILS ----
    positions: List[Dict[str, Any]] = []
    position_details: Dict[str, Dict[str, Any]] = {}

    runtime_token_status_rows: List[Dict[str, Any]] = []
    for mint_id in sorted(all_mint_ids):
        ms = state_mints.get(mint_id) or {}
        ss = status_by_mint.get(mint_id) or {}
        decimals = int(ss.get("decimals") or ms.get("decimals") or 6)
        symbol = symbol_by_mint.get(mint_id) or mint_id[:8]
        name = ss.get("name") or ""
        balance_raw = 0
        if ss.get("balance_raw") is not None:
            try:
                balance_raw = int(ss["balance_raw"])
            except (ValueError, TypeError):
                pass
        if balance_raw == 0 and ms.get("last_known_balance_raw"):
            try:
                balance_raw = int(ms["last_known_balance_raw"])
            except (ValueError, TypeError):
                pass
        balance_ui = balance_raw / (10 ** decimals) if decimals else 0

        # Source breakdown and position status from single truth layer
        lots = (ms.get("lots") or []) if isinstance(ms, dict) else []
        tx_derived_raw, bootstrap_raw, transfer_unknown_raw, _ = dt.lot_source_breakdown(lots)
        executed_steps_list = []
        sold_raw = 0
        sold_bot_raw = 0
        sold_external_raw = 0
        realized_pnl_bot_sol = 0.0
        realized_pnl_external_sol = 0.0
        for step_key, step in (ms.get("executed_steps") or {}).items():
            if isinstance(step, dict):
                amt = int(step.get("sold_raw") or 0)
                sol_out = float(step.get("sol_out") or 0)
                sold_raw += amt
                is_ext = isinstance(step_key, str) and step_key.startswith("ext_")
                if is_ext:
                    sold_external_raw += amt
                    realized_pnl_external_sol += sol_out
                else:
                    sold_bot_raw += amt
                    realized_pnl_bot_sol += sol_out
                executed_steps_list.append({"step_id": step_key, **step})
        source_breakdown = {
            "tx_derived_raw": tx_derived_raw,
            "bootstrap_snapshot_raw": bootstrap_raw,
            "transfer_unknown_raw": transfer_unknown_raw,
            "sold_raw": sold_raw,
            "sold_bot_raw": sold_bot_raw,
            "sold_external_raw": sold_external_raw,
            "realized_pnl_bot_sol": realized_pnl_bot_sol,
            "realized_pnl_external_sol": realized_pnl_external_sol,
            "current_balance_raw": balance_raw,
        }
        truth = dt.token_truth(mint_id, ms, ss, decimals=decimals, symbol=symbol, sold_raw_from_steps=sold_raw)
        entry_sol = truth.get("entry_sol_per_token")
        current_price_sol: Optional[float] = None
        liquidity_usd = truth.get("liquidity_usd")
        market = (ss.get("market") or {}).get("dexscreener") if isinstance(ss.get("market"), dict) else None
        price_native = None
        price_usd = None
        if isinstance(market, dict):
            price_native = _safe_float(market.get("price_native"))
            price_usd = _safe_float(market.get("price_usd"))
            if liquidity_usd is None:
                liquidity_usd = _safe_float(market.get("liquidity_usd"))
        # Price normalization:
        # - For regular tokens, Dexscreener price_native is token price in SOL.
        # - For stablecoins like USDC/USDT, token price in SOL should be 1 / SOL_USD.
        if symbol in stable_symbols and sol_price_usd is not None and sol_price_usd > 0:
            current_price_sol = 1.0 / sol_price_usd
        else:
            current_price_sol = price_native
        txns24h = market.get("txns24h") if isinstance(market, dict) else None
        volume24h = _safe_float(market.get("volume24h_usd")) if isinstance(market, dict) else None

        cost_basis_sol = (entry_sol * balance_ui) if (entry_sol is not None and balance_ui is not None) else None
        value_sol = (current_price_sol * balance_ui) if (current_price_sol is not None and balance_ui is not None) else None
        unrealized_sol = (value_sol - cost_basis_sol) if (value_sol is not None and cost_basis_sol is not None) else None
        total_sold_raw = sold_raw
        total_sold_ui = total_sold_raw / (10 ** decimals) if decimals else 0
        # Sellable contract: main table = Tradable now (runtime_tradable_raw)
        runtime_tradable_raw = truth.get("runtime_tradable_raw", 0)
        runtime_tradable_ui = runtime_tradable_raw / (10 ** decimals) if decimals else 0
        tx_derived_sellable_raw = truth.get("tx_derived_sellable_raw", 0)
        tx_derived_sellable_ui = tx_derived_sellable_raw / (10 ** decimals) if decimals else 0
        lot_remaining_raw = truth.get("lot_remaining_raw", 0)
        lot_remaining_ui = lot_remaining_raw / (10 ** decimals) if decimals else 0
        sellable_source = truth.get("sellable_source") or "none"
        # Balance reconciliation for display: if sum(lot.remaining) != wallet balance, flag mismatch
        # but DO NOT override wallet balance with lot sum. Wallet balance remains the source of truth.
        balance_mismatch = False
        if lots and balance_raw != lot_remaining_raw:
            balance_mismatch = True

        position_status = truth.get("position_status") or "unknown"
        realized_sol_mint = sum(float(s.get("sol_out") or 0) for s in executed_steps_list if isinstance(s, dict))
        sold_bot_raw = truth.get("sold_bot_raw", sold_bot_raw)
        sold_external_raw = truth.get("sold_external_raw", sold_external_raw)
        alerts_list = truth.get("alerts") or []
        external_inventory_only = bool(truth.get("external_inventory_only", False))
        inventory_source_label = truth.get("inventory_source_label") or (
            "external_transfer" if external_inventory_only else "bot_acquired"
        )

        # Sell readiness from runner (health_status.sell_readiness)
        sr = sell_readiness_by_mint.get(mint_id) or {}
        next_target_price = sr.get("next_target_price")
        next_step_index = sr.get("next_step_index")
        distance_to_next_target_pct = sr.get("distance_to_next_target_pct")
        sell_ready_now = sr.get("sell_ready_now", False)
        sell_blocked_reason = (sr.get("sell_blocked_reason") or "").strip()
        next_target_pct = None
        if next_target_price is not None and entry_sol and entry_sol > 0:
            next_target_pct = ((next_target_price / entry_sol) - 1.0) * 100.0
        if sell_ready_now and not sell_blocked_reason:
            next_action = "SELL NOW"
        elif sell_blocked_reason:
            next_action = f"blocked: {sell_blocked_reason}"
        elif next_target_price is not None and distance_to_next_target_pct is not None:
            next_action = f"target {next_target_price:.2e} ({distance_to_next_target_pct:+.1f}%)"
        elif next_target_price is not None:
            next_action = f"target {next_target_price:.2e}"
        elif entry_sol is None or entry_sol <= 0:
            next_action = "no entry"
        else:
            next_action = truth.get("next_action") or "no ladder"

        # Ladder progress: count only ladder step keys (1..LADDER_TOTAL_LEVELS); exclude ext_* backfill keys.
        _executed_steps = ms.get("executed_steps") or {} if isinstance(ms, dict) else {}
        _ladder_step_count = 0
        for k in _executed_steps:
            if isinstance(k, str) and not k.startswith("ext_"):
                try:
                    sid = int(k)
                    if 1 <= sid <= LADDER_TOTAL_LEVELS:
                        _ladder_step_count += 1
                except ValueError:
                    pass
        executed_levels = min(_ladder_step_count, LADDER_TOTAL_LEVELS)  # invariant: executed <= total
        last_event_ts = None
        first_event_ts = None
        for ev in events_raw:
            if ev.get("mint") and (mint_id.startswith(str(ev["mint"])) or str(ev["mint"]) in mint_id):
                if first_event_ts is None:
                    first_event_ts = ev.get("ts")
                last_event_ts = ev.get("ts")
        for ev in reversed(events_raw):
            if ev.get("mint") and (mint_id.startswith(str(ev["mint"])) or str(ev["mint"]) in mint_id):
                last_event_ts = ev.get("ts")
                break
        last_sell_at = ms.get("last_sell_at") if isinstance(ms, dict) else None
        # Derive last_sell from executed_steps when last_sell_at not set (e.g. backfill-only path).
        if last_sell_at is None and _executed_steps:
            _times = []
            for step in _executed_steps.values():
                if isinstance(step, dict) and step.get("time"):
                    _times.append(step["time"])
            if _times:
                last_sell_at = max(_times)
        # Optional: last_sell_sig / last_sell_price from the latest step (for detail panel).
        last_sell_sig = None
        last_sell_price = None
        if _executed_steps and last_sell_at:
            for step in _executed_steps.values():
                if isinstance(step, dict) and step.get("time") == last_sell_at:
                    last_sell_sig = step.get("signature") or step.get("tx_sig") or step.get("sig")
                    if step.get("price") is not None:
                        last_sell_price = _safe_float(step.get("price"))
                    break
        cooldown = ms.get("cooldown_until") if isinstance(ms, dict) else None
        paused = truth.get("paused_until")
        first_detected = None
        for lot in lots:
            if isinstance(lot, dict) and lot.get("detected_at"):
                d = lot["detected_at"]
                if first_detected is None or (d or "") < (first_detected or ""):
                    first_detected = d

        last_update_time = _file_mtime_iso(state_path) or generated_at
        runtime_token_status_rows.append({
            "symbol": symbol,
            "mint": mint_id,
            "amount_raw": balance_raw,
            "amount_ui": balance_ui,
            "entry_price": entry_sol,
            "current_price": current_price_sol,
            "value_sol": value_sol,
            "unrealized_pnl": unrealized_sol,
            "runtime_tradable_raw": runtime_tradable_raw,
            "next_target": next_target_price,
            "distance_to_target_pct": distance_to_next_target_pct,
            "ladder_step_next": next_step_index,
            "sell_ready": bool(sell_ready_now and not sell_blocked_reason),
            "blocked_reason": sell_blocked_reason or None,
            "liquidity": liquidity_usd,
            "last_event": last_event_ts or last_sell_at,
            "last_update_time": last_update_time,
        })
        row = {
            "mint": mint_id,
            "symbol": symbol,
            "name": name,
            "balance_raw": str(balance_raw),
            "balance_ui": balance_ui,
            "amount_ui": balance_ui,
            "balance_mismatch": balance_mismatch,
            "position_status": position_status,
            "alerts": alerts_list,
            "entry_sol_per_token": entry_sol,
            "cost_basis_sol": cost_basis_sol,
            "current_price_sol": current_price_sol,
            "value_sol": value_sol,
            "unrealized_pnl_sol": unrealized_sol,
            "realized_pnl_sol": realized_sol_mint,
            "realized_pnl_bot_sol": realized_pnl_bot_sol,
            "realized_pnl_external_sol": realized_pnl_external_sol,
            "total_sold_raw": total_sold_raw,
            "total_sold_ui": total_sold_ui,
            "sold_bot_raw": sold_bot_raw,
            "sold_external_raw": sold_external_raw,
            "sold_bot_ui": sold_bot_raw / (10 ** decimals) if decimals else 0,
            "sold_external_ui": sold_external_raw / (10 ** decimals) if decimals else 0,
            "runtime_tradable_raw": runtime_tradable_raw,
            "runtime_tradable_ui": runtime_tradable_ui,
            "tx_derived_sellable_raw": tx_derived_sellable_raw,
            "tx_derived_sellable_ui": tx_derived_sellable_ui,
            "lot_remaining_raw": lot_remaining_raw,
            "lot_remaining_ui": lot_remaining_ui,
            "sellable_source": sellable_source,
            "sellable_ui": runtime_tradable_ui,
            "lots_active": sum(1 for l in lots if isinstance(l, dict) and l.get("status") not in ("duplicate_explained",)),
            "lots_total": len(lots),
            "ladder_executed": executed_levels,
            "ladder_total": LADDER_TOTAL_LEVELS,
            "next_action": next_action,
            "next_target_price": next_target_price,
            "next_target_pct": next_target_pct,
            "next_step_index": next_step_index,
            "distance_to_next_target_pct": distance_to_next_target_pct,
            "sell_ready_now": sell_ready_now,
            "sell_blocked_reason": sell_blocked_reason or None,
            "bag_zero_reason": truth.get("bag_zero_reason"),
            "external_inventory_only": external_inventory_only,
            "inventory_source_label": inventory_source_label,
            "liquidity_usd": liquidity_usd,
            "liquidity_state": "healthy" if (liquidity_usd is None or liquidity_usd >= 10_000) else "low",
            "txns24h": txns24h,
            "volume24h_usd": volume24h,
            "last_event_ts": last_event_ts or last_sell_at,
            "cooldown_until": cooldown,
            "paused_until": paused,
            "tradable": ms.get("tradable"),
            "tradable_reason": ms.get("tradable_reason"),
            "ready_to_sell": bool(sell_ready_now and not sell_blocked_reason),
            "near_trigger": (
                distance_to_next_target_pct is not None
                and -5 <= distance_to_next_target_pct <= 5
                and runtime_tradable_raw > 0
            ),
            "blocked_missing_entry": "missing_entry" in alerts_list or "tx_derived_missing_entry" in alerts_list,
            "blocked_liquidity": liquidity_usd is not None and liquidity_usd < 10_000,
            "next_likely_seller": (
                runtime_tradable_raw > 0
                and distance_to_next_target_pct is not None
                and -5 <= distance_to_next_target_pct <= 5
                and not (sell_ready_now and not sell_blocked_reason)
            ),
        }
        positions.append(row)

        # Detail for expand
        lot_rows = []
        for lot in lots:
            if not isinstance(lot, dict):
                continue
            input_mint = lot.get("input_asset_mint")
            input_sym = (symbol_by_mint.get(input_mint) or (sc._short_mint(input_mint) if input_mint else None))
            swap_display = None
            if lot.get("acquired_via_swap") and (input_sym or input_mint):
                swap_display = (input_sym or sc._short_mint(input_mint)) + " → " + symbol
            lot_rows.append({
                "lot_id": lot.get("lot_id"),
                "detected_at": lot.get("detected_at"),
                "tx_signature": lot.get("tx_signature"),
                "source": lot.get("source") or lot.get("source_type"),
                "token_amount": lot.get("token_amount"),
                "remaining_amount": lot.get("remaining_amount"),
                "entry_price_sol_per_token": _safe_float(lot.get("entry_price_sol_per_token")),
                "entry_confidence": lot.get("entry_confidence") or "unknown",
                "status": lot.get("status"),
                "swap_type": lot.get("swap_type"),
                "acquired_via_swap": lot.get("acquired_via_swap", False),
                "input_asset_mint": input_mint,
                "input_asset_symbol": input_sym,
                "swap_display": swap_display,
                "input_amount": lot.get("input_amount"),
                "valuation_method": lot.get("valuation_method"),
            })
        token_events = [e for e in events_raw[-100:] if e.get("mint") and (str(mint_id).startswith(str(e["mint"])) or str(e["mint"]) in mint_id)]
        buybacks_data = ms.get("buybacks") if isinstance(ms, dict) else None
        failures_data = ms.get("failures") if isinstance(ms, dict) else None
        position_details[mint_id] = {
            "position_summary": {
                "mint": mint_id,
                "symbol": symbol,
                "wallet_amount_raw": balance_raw,
                "wallet_amount_ui": balance_ui,
                "entry_sol_per_token": entry_sol,
                "entry_unknown": truth.get("entry_unknown", entry_sol is None),
                "current_price_sol": current_price_sol,
                "realized_pnl_sol": realized_sol_mint,
                "realized_pnl_bot_sol": realized_pnl_bot_sol,
                "realized_pnl_external_sol": realized_pnl_external_sol,
                "unrealized_pnl_sol": unrealized_sol,
                "first_detected": first_detected,
                "last_activity": last_event_ts or last_sell_at,
                "balance_mismatch": balance_mismatch,
                "last_sell_at": last_sell_at,
                "last_sell_sig": last_sell_sig,
                "last_sell_price": last_sell_price,
            },
            "source_breakdown": source_breakdown,
            "sellable_breakdown": {
                "runtime_tradable_raw": runtime_tradable_raw,
                "runtime_tradable_ui": runtime_tradable_ui,
                "tx_derived_sellable_raw": tx_derived_sellable_raw,
                "tx_derived_sellable_ui": tx_derived_sellable_ui,
                "lot_remaining_raw": lot_remaining_raw,
                "lot_remaining_ui": lot_remaining_ui,
                "sellable_source": sellable_source,
            },
            "ladder_state": {
                "executed": executed_levels,
                "total": LADDER_TOTAL_LEVELS,
                "pending": max(0, LADDER_TOTAL_LEVELS - executed_levels),
                "skipped": None,
                "next_target": next_target_price,
                "next_step_index": next_step_index,
                "next_size_raw": None,
                "cooldown_until": cooldown,
                "last_sell_at": last_sell_at,
                "last_sell_sig": last_sell_sig,
                "last_sell_price": last_sell_price,
                "last_skip_reason": sell_blocked_reason or None,
                "paused_until": paused,
            },
            "buyback_state": {
                "enabled": runtime.get("buyback_enabled"),
                "total_sol_spent": buybacks_data.get("total_sol_spent") if isinstance(buybacks_data, dict) else None,
                "last_buy_at": buybacks_data.get("last_buy_at") if isinstance(buybacks_data, dict) else None,
                "last_sig": buybacks_data.get("last_sig") if isinstance(buybacks_data, dict) else None,
                "block_reason": None,
            },
            "lots": lot_rows,
            "executed_steps": executed_steps_list,
            "market": market,
            "recent_events": token_events[-20:],
            "buybacks": buybacks_data,
        }

    # ---- RECENT EVENTS (global feed, normalized for operator table; Birdeye-style swap labels) ----
    recent_events = []
    for e in events_raw[-80:]:
        mint = e.get("mint")
        sig = e.get("signature") or e.get("tx_sig") or e.get("tx_signature")
        ev = {
            "time": e.get("ts"),
            "ts": e.get("ts"),
            "token": mint,
            "mint": mint,
            "event_type": e.get("event"),
            "event": e.get("event"),
            "action": e.get("event"),
            "amount": e.get("sold_raw") or e.get("amount_raw") or e.get("token_amount_raw") or e.get("unmatched_raw"),
            "price": e.get("entry_price") or e.get("price"),
            "result": e.get("reason") or sig,
            "tx_sig": sig,
            "tx_sig_short": sc.short_mint(sig) if sig else None,
            "symbol": symbol_by_mint.get(mint) or (sc.short_mint(mint) if mint else "—"),
            "note": e.get("reason") or str({k: v for k, v in e.items() if k not in ("ts", "event", "mint")})[:120],
        }
        # Birdeye-style swap display: source → destination
        src_mint = e.get("source_mint") or e.get("input_asset_mint")
        dest_mint = e.get("destination_mint") or mint
        if src_mint and dest_mint:
            ev["swap_display"] = (symbol_by_mint.get(src_mint) or sc.short_mint(src_mint)) + " → " + (symbol_by_mint.get(dest_mint) or sc.short_mint(dest_mint))
        else:
            ev["swap_display"] = None
        ev.update({k: v for k, v in e.items() if k not in ev or ev.get(k) is None})
        recent_events.append(ev)
    recent_events.reverse()

    # ---- RUNTIME HEALTH ----
    today_prefix = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    events_today = sum(1 for e in events_raw if (e.get("ts") or "").startswith(today_prefix))
    failed_tx_today = sum(1 for e in events_raw if (e.get("ts") or "").startswith(today_prefix) and e.get("event") in ("EVENT_SELL_FAILED",))
    reconciliation_today = sum(1 for e in events_raw if (e.get("ts") or "").startswith(today_prefix) and e.get("event") in ("STATE_BALANCE_MISMATCH", "EVENT_RECONCILED", "MINT_HOLDING_EXPLANATION"))
    active_alerts_count = len(error_alerts) + sum(len(p.get("alerts") or []) for p in positions)
    header["alerts_count"] = active_alerts_count

    backfill_done = (data_dir / ".tx_backfill_done").exists() or (data_dir / "lot_tx_backfill_done").exists()
    runtime_health = {
        "bot_state": runtime_status,
        "cycle_index": cycle_index,
        "current_cycle_number": runtime.get("current_cycle_number") or runtime.get("cycles") or cycle_index,
        "loop_heartbeat_at": runtime.get("loop_heartbeat_at") or runtime.get("timestamp"),
        "last_successful_cycle_at": runtime.get("last_successful_cycle_at"),
        "last_failed_cycle_at": runtime.get("last_failed_cycle_at"),
        "rpc_ok": runtime.get("ok"),
        "rpc_latency_ms": runtime.get("rpc_latency_ms"),
        "rpc_provider_label": runtime.get("rpc_provider_label"),
        "config_profile": runtime.get("config_profile"),
        "clean_start_active": runtime.get("clean_start_active"),
        "backfill_completed": runtime.get("backfill_completed") if runtime.get("backfill_completed") is not None else backfill_done,
        "buyback_enabled": runtime.get("buyback_enabled"),
        "state_updated_at": _file_mtime_iso(state_path),
        "status_updated_at": _file_mtime_iso(status_path),
        "run_log_size": log_path.stat().st_size if log_path.exists() else None,
        "run_log_mtime": _file_mtime_iso(log_path) if log_path.exists() else None,
        "events_count": len(events_raw),
        "events_today": events_today,
        "failed_tx_today": failed_tx_today,
        "reconciliation_mismatches_today": reconciliation_today,
        "active_alerts": active_alerts_count,
        "error_alerts": error_alerts,
        "quick_links": {"run_log": "/run.log", "state_json": "/state.json", "status_json": "/status.json", "events_jsonl": "/events.jsonl"},
    }
    if not (data_dir / "events.jsonl").exists():
        runtime_health["quick_links"] = {k: v for k, v in runtime_health["quick_links"].items() if k != "events_jsonl"}

    # Wallet-level summary totals (read-only aggregation for dashboard summary bar)
    total_sold_bot_raw = 0
    total_sold_external_raw = 0
    total_runtime_tradable_raw = 0
    tradable_mints_count = 0
    blocked_mints_count = 0
    for p in positions:
        if isinstance(p, dict):
            total_sold_bot_raw += int(p.get("sold_bot_raw") or 0)
            total_sold_external_raw += int(p.get("sold_external_raw") or 0)
            total_runtime_tradable_raw += int(p.get("runtime_tradable_raw") or 0)
            if int(p.get("runtime_tradable_raw") or 0) > 0:
                tradable_mints_count += 1
            if p.get("sell_blocked_reason") or p.get("bag_zero_reason"):
                blocked_mints_count += 1
    summary_totals = {
        "total_sold_bot_raw": total_sold_bot_raw,
        "total_sold_external_raw": total_sold_external_raw,
        "total_runtime_tradable_raw": total_runtime_tradable_raw,
        "tradable_mints_count": tradable_mints_count,
        "blocked_mints_count": blocked_mints_count,
        "wallet_sol": kpis.get("wallet_sol"),
        "positions_count": kpis.get("total_tokens_count"),
        "open_positions_count": kpis.get("open_positions_count"),
        "active_lots_count": kpis.get("active_lots_count"),
    }
    # Backwards-compatible aliases for summary metrics (without _count suffix)
    summary_totals["active_lots"] = summary_totals.get("active_lots_count")
    summary_totals["tradable_mints"] = summary_totals.get("tradable_mints_count")

    # Sniper and discovery sections: derived from state, same source as _build_sniper_summary /
    # _build_discovery_section helpers. Included here so the operator dashboard HTML can consume
    # them without a separate endpoint. No trading logic is changed.
    sniper_summary = _build_sniper_summary(state if isinstance(state, dict) else None)
    sniper_pending_raw = (state or {}).get("sniper_pending_attempts") if isinstance(state, dict) else {}
    sniper_pending_list = list(sniper_pending_raw.values()) if isinstance(sniper_pending_raw, dict) else []
    sniper_decisions_list = (state or {}).get("sniper_last_decisions") if isinstance(state, dict) else []
    if not isinstance(sniper_decisions_list, list):
        sniper_decisions_list = []

    return {
        "header": header,
        "kpis": kpis,
        "summary_totals": summary_totals,
        "positions": positions,
        "position_details": position_details,
        "recent_events": recent_events,
        "runtime_health": runtime_health,
        "generated_at": generated_at,
        "symbol_by_mint": symbol_by_mint,
        "runtime_token_status": {
            "rows": runtime_token_status_rows,
            "sort_options": ["closest_to_sell", "highest_value", "highest_pnl", "largest_position"],
        },
        "sniper_summary": sniper_summary,
        "sniper_pending_attempts": sniper_pending_list,
        "sniper_recent_decisions": sniper_decisions_list[-20:],
        "discovery": _build_discovery_section(state if isinstance(state, dict) else None),
    }


def get_dashboard_payload_cached(data_dir: Path) -> Dict[str, Any]:
    """Return dashboard payload, from cache if valid (TTL 1s), else rebuild. Thread-safe."""
    global _cached_payload, _cached_at, _cached_data_dir
    now = time.monotonic()
    with _cache_lock:
        if (
            _cached_payload is not None
            and _cached_data_dir is not None
            and _cached_data_dir == data_dir.resolve()
            and (now - _cached_at) <= _CACHE_TTL_SEC
        ):
            return _cached_payload
        payload = build_operator_dashboard_payload(data_dir)
        _cached_payload = payload
        _cached_at = now
        _cached_data_dir = data_dir.resolve()
        try:
            from .integration.event_bus import emit
            journal_path = data_dir / "events.jsonl"
            emit(journal_path, "DASHBOARD_UPDATED", "Dashboard", "info", short_message="Dashboard payload rebuilt", send_telegram=False)
        except Exception:
            pass
        return payload


def invalidate_dashboard_cache() -> None:
    """Clear dashboard response cache so next request reflects latest state. Call after state save."""
    global _cached_payload, _cached_at, _cached_data_dir
    with _cache_lock:
        _cached_payload = None
        _cached_at = 0.0
        _cached_data_dir = None


def _no_cache_headers() -> List[tuple]:
    """Headers so browsers and proxies do not cache runtime data. Use for all runtime JSON/HTML responses."""
    return [
        ("Cache-Control", "no-store, no-cache, must-revalidate"),
        ("Pragma", "no-cache"),
        ("Expires", "0"),
    ]


class _DashboardHandler(BaseHTTPRequestHandler):
    data_dir: Path = Path(".")
    event_journal_path: Optional[Path] = None

    def _send_no_cache_headers(self) -> None:
        for name, value in _no_cache_headers():
            self.send_header(name, value)

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        t0 = time.monotonic()
        path = self.path.split("?")[0].rstrip("/")
        if path == "" or path == "/":
            # Serve the bundled dashboard HTML from the package's static directory.
            # The HTML lives at mint_ladder_bot/static/dashboard.html (source-controlled).
            # Runtime data_dir is for generated artifacts only (state.json, events.jsonl, etc.).
            _static_html = Path(__file__).parent / "static" / "dashboard.html"
            if _static_html.exists():
                try:
                    body = _static_html.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self._send_no_cache_headers()
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    logger.warning("Serve dashboard.html failed: %s", e)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self._send_no_cache_headers()
                    self.end_headers()
                    self.wfile.write(b"OK")
            else:
                logger.warning("dashboard.html not found at %s", _static_html)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self._send_no_cache_headers()
                self.end_headers()
                self.wfile.write(b"OK")
            logger.info("GET / 200 latency_ms=%.0f", (time.monotonic() - t0) * 1000)
            return
        if path == "/state.json":
            state_path = self.data_dir / "state.json"
            if state_path.exists():
                try:
                    body = state_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self._send_no_cache_headers()
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    logger.warning("Serve state.json failed: %s", e)
                    self.send_response(500)
                    self._send_no_cache_headers()
                    self.end_headers()
            else:
                self.send_response(404)
                self._send_no_cache_headers()
                self.end_headers()
            return
        if path == "/status.json":
            status_path = self.data_dir / "status.json"
            if status_path.exists():
                try:
                    body = status_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self._send_no_cache_headers()
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    logger.warning("Serve status.json failed: %s", e)
                    self.send_response(500)
                    self._send_no_cache_headers()
                    self.end_headers()
            else:
                self.send_response(404)
                self._send_no_cache_headers()
                self.end_headers()
            return
        if path == "/run.log":
            log_path = self.data_dir / "run.log"
            if log_path.exists():
                try:
                    body = log_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self._send_no_cache_headers()
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    logger.warning("Serve run.log failed: %s", e)
                    self.send_response(500)
                    self._send_no_cache_headers()
                    self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self._send_no_cache_headers()
                self.end_headers()
                self.wfile.write(b"")
            return
        if path == "/events.jsonl":
            events_path = self.data_dir / "events.jsonl"
            if events_path.exists():
                try:
                    body = events_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self._send_no_cache_headers()
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    logger.warning("Serve events.jsonl failed: %s", e)
                    self.send_response(500)
                    self._send_no_cache_headers()
                    self.end_headers()
            else:
                self.send_response(404)
                self._send_no_cache_headers()
                self.end_headers()
            return
        if path == "/runtime/log-tail":
            try:
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                lines_param = 80
                for part in query.split("&"):
                    if part.startswith("lines="):
                        try:
                            lines_param = min(200, max(10, int(part[6:].strip())))
                        except ValueError:
                            pass
                        break
                log_path = get_run_log_path()
                if not log_path.exists():
                    body = b"Log unavailable (run.log not found)"
                else:
                    raw = log_path.read_text(encoding="utf-8", errors="replace")
                    tail_lines = raw.splitlines()[-lines_param:]
                    body = "\n".join(tail_lines).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._send_no_cache_headers()
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                logger.warning("Serve log-tail failed: %s", e)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self._send_no_cache_headers()
                self.end_headers()
                self.wfile.write(b"Log unavailable")
            return
        if path == "/runtime/dashboard":
            try:
                payload = get_dashboard_payload_cached(self.data_dir)
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._send_no_cache_headers()
                self.end_headers()
                self.wfile.write(body)
                latency_ms = (time.monotonic() - t0) * 1000
                logger.info("GET /runtime/dashboard 200 latency_ms=%.0f", latency_ms)
                _maybe_emit_data_validated(payload, self.event_journal_path)
            except Exception as e:
                logger.exception("Dashboard build failed: %s", e)
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self._send_no_cache_headers()
                self.end_headers()
                self.wfile.write(b"Internal server error")
                logger.info("GET /runtime/dashboard 500 latency_ms=%.0f", (time.monotonic() - t0) * 1000)
            return
        self.send_response(404)
        self._send_no_cache_headers()
        self.end_headers()
        logger.info("GET %s 404 latency_ms=%.0f", self.path, (time.monotonic() - t0) * 1000)

    def do_HEAD(self) -> None:
        if self.path == "/" or self.path == "":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            return
        if self.path.rstrip("/") == "/runtime/dashboard":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


def _maybe_emit_data_validated(payload: Dict[str, Any], journal_path: Optional[Path]) -> None:
    """Append DASHBOARD_DATA_VALIDATED to event journal if path set. Supports operator payload shape."""
    if not journal_path:
        return
    try:
        from .events import append_event
        rh = payload.get("runtime_health") or {}
        meta = {
            "generated_at": payload.get("generated_at"),
            "cycle_index": payload.get("cycle_index") or rh.get("cycle_index"),
            "positions_count": len(payload.get("positions") or []),
            "recent_events_count": len(payload.get("recent_events") or []),
            "alerts_count": len(rh.get("error_alerts") or []),
        }
        append_event(journal_path, "DASHBOARD_DATA_VALIDATED", meta)
    except Exception as e:
        logger.debug("DASHBOARD_DATA_VALIDATED append failed: %s", e)


def run_standalone(data_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run dashboard HTTP server in the current process (blocking). For CLI/operational use."""
    data_dir = data_dir.resolve()
    _DashboardHandler.data_dir = data_dir
    _DashboardHandler.event_journal_path = data_dir / "events.jsonl"
    server = HTTPServer((host, port), _DashboardHandler)
    logger.info("Dashboard server listening on http://%s:%s data_dir=%s", host, port, data_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Mint Ladder runtime dashboard HTTP server")
    parser.add_argument("--data-dir", type=Path, default=Path("runtime/projects/mint_ladder_bot"), help="Runtime project directory (state, status, health, run.log)")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind (default 8765)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    data_dir = args.data_dir if args.data_dir.is_absolute() else root / args.data_dir
    run_standalone(data_dir=data_dir, host=args.host, port=args.port)

