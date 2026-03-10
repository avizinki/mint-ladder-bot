from __future__ import annotations

import itertools
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set, Tuple
from urllib.parse import urlparse

import httpx

from .config import Config
from .jupiter import JupiterError, get_quote, get_quote_quick, get_swap_tx
from .models import (
    SolBalance,
    BootstrapInfo,
    LiquidityCapInfo,
    LotInfo,
    MintStatus,
    PriceSample,
    PumpInfo,
    RuntimeMintState,
    RuntimeState,
    StatusFile,
    StepExecutionInfo,
    VolatilityInfo,
)
from .tx_infer import _parse_sol_delta_lamports, _parse_token_deltas_for_mints, parse_sell_events_from_tx
from .rpc import RpcClient
from .state import ensure_mint_state, load_state, save_state_atomic, validate_state_schema
from .sniper_engine.service import SniperService
from .risk_engine import RiskLimits, block_execution_reason
from .execution_engine import execute_swap, ExecutionResult
from .events import (
    append_event,
    add_processed_fingerprint,
    add_processed_signature,
    is_duplicate_fingerprint,
    make_fingerprint,
    make_unresolved_delta_fingerprint,
    RISK_BLOCK,
    EVENT_LOT_CREATED,
    EVENT_LOT_CREATED_TX_EXACT,
    EVENT_LOT_CREATED_SNAPSHOT,
    EVENT_BUY_PRICE_UNKNOWN,
    EVENT_BUY_BACKFILL_CREATED,
    EVENT_BUY_BACKFILL_SKIPPED,
    EVENT_RECONCILED,
    EVENT_SELL_SENT,
    EVENT_SELL_CONFIRMED,
    EVENT_SELL_FAILED,
    EVENT_PROTECTION_ARMED,
    EVENT_TP_HIT,
    EVENT_STOP_HIT,
    EVENT_CIRCUIT_BREAKER,
    EVENT_BOT_SELL_ACCOUNTED,
    EVENT_EXTERNAL_SELL_ACCOUNTED,
    EVENT_SELL_ACCOUNTING_INVARIANT_BROKEN,
    EVENT_MINT_DETECTED,
    EVENT_BALANCE_INCREASE_UNMATCHED,
    EVENT_BUY_DETECTED_NO_TX,
    EVENT_UNRESOLVED_BALANCE_DELTA,
    EVENT_SNAPSHOT_FALLBACK_USED,
    EXTERNAL_TX_OBSERVED,
    UNCLASSIFIED_EXTERNAL_TX,
    UNEXPLAINED_WALLET_CHANGE,
    BUY_SENT,
    BUY_CONFIRMED,
    BUY_FAILED,
    TOKEN_FILTER_APPROVED,
    TOKEN_FILTER_REJECTED,
    LADDER_ARMED,
    EVENT_MINT_RECONCILIATION_PAUSED,
    EVENT_MINT_RECONCILIATION_RECOVERED,
    MINT_PAUSED,
    MANUAL_OVERRIDE_CONSUMED,
    MANUAL_OVERRIDE_BYPASS_ENABLED,
    MANUAL_OVERRIDE_BYPASS_DISABLED,
)
from .bag_zero_reason import classify_bag_zero_reason
from .lot_invariants import check_duplicate_lot_for_tx, check_lot_invariants
from .tx_lot_engine import run_tx_first_lot_engine
from .health import write_health_status


def _build_health_runtime_info(
    *,
    cycle: int,
    rpc_latency_ms: float,
    paused_mints: int,
    clean_start: bool,
    backfill_completed: bool,
    config: Config,
    sell_readiness: Dict[str, Dict[str, Any]],
    monitor_only: bool,
    trading_ok: bool,
    last_error: Optional[str] = None,
    rpc_failures_consecutive: int = 0,
    global_trading_paused_until: Optional[datetime] = None,
    cycle_mismatch_first_detected_at_cycle: Optional[int] = None,
    sells_failed: int = 0,
) -> Dict[str, Any]:
    """
    Construct runtime_info payload for health_status.json.

    Read-only: does not mutate state; safe to test in isolation.
    """
    now_iso = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    ep = (getattr(config, "rpc_endpoint", "") or "").lower()
    if "helius" in ep:
        rpc_label = "helius"
    elif "triton" in ep:
        rpc_label = "triton"
    else:
        rpc_label = "rpc"
    runner_mode = (
        "blocked" if not trading_ok
        else "monitor_only" if monitor_only
        else "live"
    )
    return {
        "cycles": cycle,
        "rpc_latency_ms": rpc_latency_ms,
        "errors": [] if last_error is None else [last_error],
        "paused_mints": paused_mints,
        "loop_heartbeat_at": now_iso,
        "last_successful_cycle_at": now_iso,
        "last_failed_cycle_at": None,
        "current_cycle_number": cycle,
        "config_profile": os.getenv("CONFIG_PROFILE", "").strip() or "default",
        "clean_start_active": clean_start,
        "backfill_completed": backfill_completed,
        "rpc_provider_label": rpc_label,
        "buyback_enabled": getattr(config, "buyback_enabled", False),
        "sell_readiness": sell_readiness,
        "runner_mode": runner_mode,
        "process_state": "running",
        "last_error": last_error,
        "rpc_failures_consecutive": rpc_failures_consecutive,
        "global_trading_paused_until": (
            global_trading_paused_until.isoformat().replace("+00:00", "Z")
            if isinstance(global_trading_paused_until, datetime)
            else None
        ),
        "cycle_mismatch_first_detected_at_cycle": cycle_mismatch_first_detected_at_cycle,
        "sells_failed": sells_failed,
    }


def _build_startup_summary(
    *,
    wallet: str,
    run_mode: str,
    project_runtime_dir: Path,
    state_path: Path,
    status_path: Path,
    tradable_mints: int,
    bootstrap_pending_mints: int,
    paused_mints: int,
    stop_paths: str,
) -> Dict[str, Any]:
    """
    Package startup summary fields for operator logs.

    Pure: does not perform any business logic, only packages already-computed facts.
    """
    return {
        "wallet": wallet,
        "run_mode": run_mode,
        "project_runtime_dir": str(project_runtime_dir),
        "state_file": str(state_path.resolve()),
        "status_file": str(status_path.resolve()),
        "tradable_mints": tradable_mints,
        "bootstrap_pending_mints": bootstrap_pending_mints,
        "paused_mints": paused_mints,
        "stop_paths": stop_paths,
    }


def _build_cycle_summary_fields(
    *,
    cycle: int,
    cycle_duration_ms: float,
    rpc_latency_ms: float,
    sells_ok: int,
    sells_fail: int,
    buybacks_ok: int,
    buybacks_fail: int,
    paused_mints: int,
    liquidity_skips: int,
    no_step: int,
    price_none: int,
    below_target: int,
    hourcap_skip: int,
    min_trade_skip: int,
    display_pending: int,
    trading_disabled: bool,
) -> Dict[str, Any]:
    """
    Package numeric/string cycle summary fields for operator logs.

    Pure: accepts already-decided facts and returns a dict suitable for logging/monitoring.
    """
    return {
        "cycle": cycle,
        "cycle_duration_ms": cycle_duration_ms,
        "rpc_latency_ms": rpc_latency_ms,
        "sells_ok": sells_ok,
        "sells_fail": sells_fail,
        "buybacks_ok": buybacks_ok,
        "buybacks_fail": buybacks_fail,
        "paused_mints": paused_mints,
        "liquidity_skips": liquidity_skips,
        "no_step": no_step,
        "price_none": price_none,
        "below_target": below_target,
        "hourcap_skip": hourcap_skip,
        "min_trade_skip": min_trade_skip,
        "display_pending": display_pending,
        "trading_disabled": trading_disabled,
    }


def _handle_rpc_failure(
    run_state: Dict[str, Any],
    config: Config,
    event_journal_path: Optional[Path],
) -> None:
    """
    Bounded RPC failure escalation.

    - Increments rpc_failures_consecutive.
    - When threshold is reached, sets global_trading_paused_until for a cooldown
      period and emits a circuit-breaker event (if journal is present).
    """
    from .events import append_event, EVENT_CIRCUIT_BREAKER

    count = run_state.get("rpc_failures_consecutive", 0) + 1
    run_state["rpc_failures_consecutive"] = count
    threshold = getattr(config, "rpc_failures_threshold", 3) or 3
    cooldown_sec = getattr(config, "rpc_cooldown_sec", 60) or 60

    if count >= threshold and run_state.get("global_trading_paused_until") is None:
        paused_until = datetime.now(tz=timezone.utc) + timedelta(seconds=cooldown_sec)
        run_state["global_trading_paused_until"] = paused_until
        if event_journal_path is not None:
            try:
                append_event(
                    event_journal_path,
                    EVENT_CIRCUIT_BREAKER,
                    {
                        "reason": "rpc_failures",
                        "count": count,
                        "paused_until": str(paused_until),
                    },
                )
            except Exception:
                pass


def _compute_trading_disabled(
    config: Config,
    stop_active: bool,
    global_pause_until: Optional[datetime],
    now_utc: datetime,
) -> bool:
    """
    Pure helper for trading-disabled gate.

    Degraded mode (global_pause_until in the future) is treated as trading-disabled,
    but the process can continue monitoring.
    """
    trading_enabled = getattr(config, "trading_enabled", False)
    live_trading = getattr(config, "live_trading", False)
    env_disabled = getattr(config, "trading_disabled_env", False)
    paused = global_pause_until is not None and now_utc < global_pause_until
    return (not trading_enabled) or (not live_trading) or stop_active or env_disabled or paused
from .strategy import (
    DynamicContext,
    LadderStep,
    build_dynamic_ladder_for_mint,
    compute_trading_bag,
)
from .wallet import WalletError, sign_swap_tx
from . import wallet_manager


logger = logging.getLogger(__name__)

WSOL_MINT = "So11111111111111111111111111111111111111112"


def _notify_founder(
    project_root: Optional[Path],
    message: str,
    title: str = "Mint Ladder",
    critical: bool = False,
) -> None:
    """Run tools/notify_founder.py (throttled unless critical). No-op if project_root is None or script missing."""
    if not project_root or not message or not message.strip():
        return
    script = project_root / "tools" / "notify_founder.py"
    if not script.exists():
        return
    try:
        import subprocess
        cmd = [str(script), message[:500], title]
        if critical:
            cmd = [str(script), "--critical", message[:500], title]
        subprocess.run(
            cmd,
            cwd=str(project_root),
            capture_output=True,
            timeout=10,
            env={**os.environ},
        )
    except Exception as exc:
        logger.debug("notify_founder failed: %s", exc)
BuybackResult = Literal["skipped", "executed", "failed"]


def _short_mint(mint: str, length: int = 8) -> str:
    """Return last `length` chars of mint for compact display."""
    if len(mint) <= length:
        return mint
    return "…" + mint[-length:]


def _pair_name(m: MintStatus) -> str:
    """Display name for logs: symbol, then name, then short mint."""
    out = (m.symbol or m.name or _short_mint(m.mint)).strip()
    return out or _short_mint(m.mint)


MAX_CONSECUTIVE_FAILURES = 3
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens"

ENTRY_PRICE_MIN = 1e-12
ENTRY_PRICE_MAX = 1e3

QUOTE_MAX_AGE_SEC = 5
MAX_SELL_BAG_FRACTION_PER_24H = 2.0
RPC_FAILURES_THRESHOLD = 5
# Entry price pipeline: scan until matching tx or this many signatures (CEO directive).
ENTRY_SCAN_MAX_SIGNATURES = 300
RPC_COOLDOWN_SEC = 120
STOP_FILE = "STOP"
BALANCE_RECONCILE_TOLERANCE = 0.05  # 5% mismatch for post-swap reconciliation
STARTUP_VALIDATION_PAUSE_MINUTES = 60
CONFIRM_UNCERTAIN_PAUSE_MINUTES = 5
# Reconciliation pause: how many consecutive mismatches before per-mint pause, and for how long.
RECONCILE_MISMATCH_CONSECUTIVE_CYCLES = 3
RECONCILE_MISMATCH_PAUSE_MINUTES = 60

# --- Risk guard constants (single reference for Risk review; see also config for tunables) ---
# STOP_FILE: kill-switch filename; paths checked: Path.cwd() / STOP_FILE, state_path.parent / STOP_FILE.
# QUOTE_MAX_AGE_SEC: max age of quote before swap (staleness guard); overridden by config.quote_max_age_sec when set.
# MAX_SELL_BAG_FRACTION_PER_24H: per-24h sell cap (bag fraction); overridden by config.max_sell_bag_fraction_per_24h.
# RPC_FAILURES_THRESHOLD: consecutive RPC failures before global pause; overridden by config.rpc_failures_threshold.
# RPC_COOLDOWN_SEC: global trading pause duration after threshold; overridden by config.rpc_cooldown_sec.
# MAX_CONSECUTIVE_FAILURES: per-mint consecutive failures before per-mint pause; overridden by config.max_consecutive_failures.
# Liquidity floor: 1000 USD in _filter_tradable_and_bootstrap_mints (mints below excluded from tradable set).


def _fracture_chunks(amount_raw: int, n: int) -> List[int]:
    """Split amount_raw into n chunks (n in 1..3); sum of chunks equals amount_raw."""
    if n <= 1 or amount_raw <= 0:
        return [amount_raw] if amount_raw > 0 else []
    n = min(max(n, 2), 3)
    base = amount_raw // n
    remainder = amount_raw % n
    return [base + (1 if i < remainder else 0) for i in range(n)]


def validate_entry_price(entry_price: float) -> bool:
    """
    Return True if entry_price is in a valid range for trading.
    Rejects zero/negative, dust, and implausibly large values (parsing/inference errors).
    """
    if entry_price <= 0:
        return False
    if entry_price < ENTRY_PRICE_MIN:
        return False
    if entry_price > ENTRY_PRICE_MAX:
        return False
    return True


def _fetch_live_dexscreener_price_native(mint: str) -> Optional[float]:
    """Fetch current SOL price for a mint from DexScreener. Returns None on failure."""
    try:
        resp = httpx.get(f"{DEXSCREENER_TOKENS_URL}/{mint}", timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None
        # Prefer SOL/WSOL pair with highest liquidity
        def liq(p):
            try:
                return float((p.get("liquidity") or {}).get("usd") or 0)
            except (TypeError, ValueError):
                return 0.0
        best = max(pairs, key=liq)
        price = best.get("priceNative")
        if price is None:
            return None
        return float(price)
    except Exception:
        return None


def _filter_tradable_and_bootstrap_mints(
    status: StatusFile,
    state: RuntimeState,
) -> Tuple[List[MintStatus], List[MintStatus]]:
    """
    Returns (tradable_mints, bootstrap_pending_mints).
    Tradable = valid entry from status or from state (bootstrap completed).
    Bootstrap_pending = unknown/invalid entry and not yet bootstrapped.
    """
    tradable: List[MintStatus] = []
    bootstrap_pending: List[MintStatus] = []
    min_liq_track = 1000.0  # same as MIN_LIQUIDITY_USD_FOR_TRACK default
    for m in status.mints:
        if m.balance_ui <= 0:
            continue
        if m.mint == WSOL_MINT:
            continue
        ds = m.market.dexscreener if m.market else None
        liquidity_usd = ds.liquidity_usd if ds is not None else None
        # Already-tracked mints (in state) stay in the list so we keep refreshing balance and showing in live-status
        already_tracked = state.mints.get(m.mint) is not None
        if not already_tracked and (liquidity_usd is None or liquidity_usd < min_liq_track):
            logger.warning(
                "Mint skipped: liquidity too low (mint=%s symbol=%s liquidity_usd=%s)",
                m.mint,
                m.symbol or "",
                liquidity_usd if liquidity_usd is not None else "None",
            )
            continue
        entry_source = getattr(m.entry, "entry_source", None) or "unknown"
        if entry_source == "unknown":
            # CEO directive: unknown entry => non-tradable
            bootstrap_pending.append(m)
            continue
        entry_price = m.entry.entry_price_sol_per_token
        # Clean-trading invariant: mint must have tx-derived lots before it can
        # be considered tradable. Prevents pure snapshot / transfer-only
        # inventory from silently entering the tradable set.
        ms = state.mints.get(m.mint)
        lots = getattr(ms, "lots", None) or []
        has_tx_lot = any(
            getattr(l, "source", "") in ("tx_exact", "tx_parsed") for l in lots
        )
        if validate_entry_price(entry_price) and has_tx_lot:
            tradable.append(m)
            continue
        ms = state.mints.get(m.mint)
        if (
            ms is not None
            and validate_entry_price(ms.entry_price_sol_per_token)
            and any(
                getattr(l, "source", "") in ("tx_exact", "tx_parsed")
                for l in (getattr(ms, "lots", None) or [])
            )
        ):
            tradable.append(m)
            continue
        if entry_price <= 0:
            logger.warning(
                "ENTRY_PRICE_INVALID mint=%s symbol=%s entry_price=%.6e tradable=False",
                m.mint[:12],
                m.symbol or "",
                entry_price,
            )
        if ms is not None and getattr(ms, "bootstrap", None) is not None and ms.bootstrap.bootstrap_completed_at is not None:
            logger.warning(
                "Mint %s bootstrap completed but entry still invalid (%.6e); skipping ladder.",
                _pair_name(m),
                getattr(ms, "entry_price_sol_per_token", 0),
            )
            continue
        bootstrap_pending.append(m)
    return tradable, bootstrap_pending


def _record_price_sample(
    mint_state: RuntimeMintState,
    price: float,
) -> None:
    """Append a price sample to the mint's history, trimming to a reasonable horizon."""
    if price <= 0:
        return
    now = datetime.now(tz=timezone.utc)
    samples = list(mint_state.price_history)
    samples.append(PriceSample(t=now, price=price))
    # Keep only ~15 minutes of history to bound state size.
    cutoff = now - timedelta(minutes=15)
    mint_state.price_history = [s for s in samples if s.t >= cutoff]


def _get_current_price_sol(
    mint_status: MintStatus,
    runtime_state: RuntimeMintState,
    rpc: RpcClient,
    config: Config,
    status_created_at: datetime,
) -> Optional[float]:
    """
    Get current price in SOL per token using Jupiter. DexScreener is used only
    as a last-resort fallback gate and not for actual swap sizing.
    """

    trading_bag_raw = int(runtime_state.trading_bag_raw)
    if trading_bag_raw <= 0:
        return None
    probe_amount_raw = max(trading_bag_raw // 100, 1)

    # Use quick quote (single attempt, short timeout) so the run loop does not block.
    quote = get_quote_quick(
        input_mint=mint_status.mint,
        output_mint=WSOL_MINT,
        amount_raw=probe_amount_raw,
        slippage_bps=config.slippage_bps,
        config=config,
        timeout_s=8.0,
    )
    if quote:
        out_amount = int(quote.get("outAmount", 0))
        if out_amount > 0:
            sol_out = out_amount / 1e9
            price = sol_out / (probe_amount_raw / (10 ** mint_status.decimals))
            _record_price_sample(runtime_state, price)
            return price
    else:
        logger.debug("Jupiter quick price failed for %s", _pair_name(mint_status))

    # Fallback: fetch live DexScreener price so we never use stale snapshot data.
    live_price = _fetch_live_dexscreener_price_native(mint_status.mint)
    if live_price is not None and live_price > 0:
        logger.debug("Using DexScreener fallback price for %s", _pair_name(mint_status))
        _record_price_sample(runtime_state, live_price)
        return live_price

    # Last resort: snapshot DexScreener only if fresh enough.
    ds = mint_status.market.dexscreener
    if ds.price_native is not None and ds.price_native > 0:
        age = datetime.now(tz=timezone.utc) - status_created_at
        if age.total_seconds() <= config.price_stale_threshold_sec:
            _record_price_sample(runtime_state, ds.price_native)
            return ds.price_native
        logger.debug(
            "DexScreener snapshot for %s is stale (%.0fs); live fetch failed.",
            _pair_name(mint_status),
            age.total_seconds(),
        )
    return None


def _enforce_liquidity_guard(mint_status: MintStatus, config: Config) -> bool:
    ds = mint_status.market.dexscreener
    if ds.liquidity_usd is not None and ds.liquidity_usd < config.liquidity_warn_threshold_usd:
        logger.warning(
            "Mint %s has low liquidity (%.2f USD) on DexScreener; skipping.",
            _pair_name(mint_status),
            ds.liquidity_usd,
        )
        return False
    return True


def _check_reanchor(
    state: RuntimeState,
    tradable_mints: List[MintStatus],
    cycle_prices: Dict[str, float],
    config: Config,
) -> None:
    """If price sustained above 1.5x working entry for N cycles and cooldown elapsed, raise working entry (re-anchor)."""
    now = datetime.now(tz=timezone.utc)
    cooldown_sec = config.reanchor_cooldown_hours * 3600.0
    for m in tradable_mints:
        ms = state.mints.get(m.mint)
        if ms is None:
            continue
        current_price = cycle_prices.get(m.mint)
        if current_price is None:
            continue
        working = _working_entry(ms)
        if current_price >= working:
            ms.cycles_price_above_working = getattr(ms, "cycles_price_above_working", 0) + 1
        else:
            ms.cycles_price_above_working = 0
            continue
        if len(ms.executed_steps) < 1:
            continue
        if current_price < working * 1.5:
            continue
        if getattr(ms, "cycles_price_above_working", 0) < config.reanchor_cycles_required:
            continue
        if _is_paused(ms):
            continue
        if not _enforce_liquidity_guard(m, config):
            continue
        last_at = getattr(ms, "last_reanchor_at", None)
        if last_at is not None:
            elapsed = (now - last_at).total_seconds()
            if elapsed < cooldown_sec:
                continue
        reanchor_times = getattr(ms, "reanchor_at_times", None) or []
        cutoff_24h = now - timedelta(hours=24)
        reanchor_times_24h = [t for t in reanchor_times if t >= cutoff_24h]
        if len(reanchor_times_24h) >= config.reanchor_max_per_24h:
            continue
        new_working = max(working, current_price * 0.85)
        if new_working <= working:
            continue
        logger.info(
            "REANCHOR mint=%s old=%.6e new=%.6e reason=price_sustained_above_1.5x",
            m.mint,
            working,
            new_working,
        )
        ms.working_entry_price_sol_per_token = new_working
        ms.last_reanchor_at = now
        ms.reanchor_count = getattr(ms, "reanchor_count", 0) + 1
        reanchor_times_24h.append(now)
        ms.reanchor_at_times = reanchor_times_24h
        ms.cycles_price_above_working = 0


def _update_volatility_and_momentum(
    mint_status: MintStatus,
    mint_state: RuntimeMintState,
) -> None:
    """
    Derive simple volatility and momentum regimes from recent price history and DexScreener.

    - Volatility: based on 1m/5m/15m absolute returns.
    - Momentum: price trend + DexScreener buy/sell imbalance.
    """

    history = mint_state.price_history
    if not history:
        return

    now = datetime.now(tz=timezone.utc)

    def _return_over(window_min: float) -> Optional[float]:
        cutoff = now - timedelta(minutes=window_min)
        past = None
        for sample in history:
            if sample.t <= cutoff:  # type: ignore[attr-defined]
                past = sample
        if past is None:
            return None
        try:
            return (history[-1].price - past.price) / past.price  # type: ignore[attr-defined]
        except Exception:
            return None

    r1 = _return_over(1.0)
    r5 = _return_over(5.0)
    r15 = _return_over(15.0)

    vol = mint_state.volatility or VolatilityInfo()
    vol.realized_1m = r1
    vol.realized_5m = r5
    vol.realized_15m = r15

    # Classify regime using simple thresholds.
    abs_1m = abs(r1) if r1 is not None else 0.0
    abs_5m = abs(r5) if r5 is not None else 0.0
    abs_15m = abs(r15) if r15 is not None else 0.0
    if abs_1m > 0.06 or abs_5m > 0.18 or abs_15m > 0.30:
        vol.regime = "high"
    elif abs_15m < 0.10 and abs_5m < 0.06:
        vol.regime = "low"
    else:
        vol.regime = "medium"

    mint_state.volatility = vol

    # Momentum: combine recent price direction with 24h buy/sell counts.
    ds = mint_status.market.dexscreener
    txs = ds.txns24h
    buys = txs.buys or 0
    sells = txs.sells or 0
    flow_score = 0.0
    total = buys + sells
    if total > 0:
        flow_score = (buys - sells) / float(total)

    # Recent price direction contribution.
    dir_score = 0.0
    if r5 is not None:
        dir_score = max(min(r5 * 10.0, 1.0), -1.0)

    momentum_score = max(min((flow_score * 0.5) + (dir_score * 0.5), 1.0), -1.0)

    mom = mint_state.momentum
    mom.score = momentum_score
    if momentum_score > 0.3:
        mom.regime = "strong"
    elif momentum_score < -0.3:
        mom.regime = "weak"
    else:
        mom.regime = "neutral"

    mint_state.momentum = mom


def _compute_pump_info(mint_state: RuntimeMintState, config: Config) -> PumpInfo:
    """
    Detect short-term pump from price_history: positive return over 1m or 5m
    above configured thresholds. Used for mode/guards later; no ladder change.
    """
    history = mint_state.price_history
    if not history:
        return PumpInfo(detected=False)
    now = datetime.now(tz=timezone.utc)
    current = history[-1].price

    def _return_over(window_min: float) -> Optional[float]:
        cutoff = now - timedelta(minutes=window_min)
        past = None
        for s in history:
            if s.t <= cutoff:
                past = s
        if past is None or past.price <= 0:
            return None
        return (current - past.price) / past.price

    r1 = _return_over(1.0)
    r5 = _return_over(5.0)
    th1 = config.pump_threshold_1m_pct / 100.0
    th5 = config.pump_threshold_5m_pct / 100.0
    detected = (r1 is not None and r1 >= th1) or (r5 is not None and r5 >= th5)
    return PumpInfo(detected=detected, return_1m=r1, return_5m=r5)


def _update_liquidity_cap(
    mint_status: MintStatus,
    mint_state: RuntimeMintState,
) -> None:
    """
    Set an upper bound on per-step size based on LP depth.

    Uses DexScreener liquidity (USD) and price (USD) to cap at a small
    fraction of LP (typically 0.2%–0.5%), depending on how strong liquidity is.
    """

    ds = mint_status.market.dexscreener
    liq = ds.liquidity_usd
    price_usd = ds.price_usd
    cap = LiquidityCapInfo()
    if liq is None or not price_usd or price_usd <= 0:
        # No reliable LP data; leave cap unset so caller can apply a conservative fallback.
        mint_state.liquidity_cap = cap
        return

    try:
        # Stronger LP -> allow up to ~0.5%, otherwise ~0.2%.
        if liq >= 5_000_000:
            frac = 0.005
        elif liq >= 1_000_000:
            frac = 0.003
        else:
            frac = 0.002
        safe_value_usd = liq * frac
        max_tokens = safe_value_usd / price_usd
        cap.max_sell_raw = int(max_tokens * (10 ** mint_status.decimals))
    except Exception:
        cap.max_sell_raw = None

    mint_state.liquidity_cap = cap


def _working_entry(mint_state: RuntimeMintState) -> float:
    """Return working entry price for ladder/targets; falls back to entry_price_sol_per_token or first valid lot entry.
    Never use mint-level market/bootstrap entry when all tx-derived lots have unknown entry (invariant: do not treat as exact).
    """
    w = getattr(mint_state, "working_entry_price_sol_per_token", None)
    if w is not None and validate_entry_price(w):
        return w
    # Prefer first valid lot entry (tx-derived) so we never use market for ladder when lots have null entry.
    for lot in getattr(mint_state, "lots", None) or []:
        if getattr(lot, "source", None) in ("tx_exact", "tx_parsed"):
            lp = getattr(lot, "entry_price_sol_per_token", None)
            if lp is not None and validate_entry_price(lp):
                return lp
    ep = getattr(mint_state, "entry_price_sol_per_token", None) or 0
    # Do not use mint-level entry for ladder when it is from market bootstrap and no lot has valid entry.
    if validate_entry_price(ep):
        if getattr(mint_state, "bootstrap_from_market", False) or getattr(mint_state, "entry_source", None) == "market_bootstrap":
            has_any_tx_lot = any(
                getattr(lot, "source", None) in ("tx_exact", "tx_parsed")
                for lot in getattr(mint_state, "lots", None) or []
            )
            if has_any_tx_lot:
                return 0.0  # Unknown entry: do not use market for ladder.
        return ep
    return ep


def _next_unexecuted_step(
    steps: List[LadderStep],
    mint_state,
) -> Optional[Tuple[LadderStep, str]]:
    for step in steps:
        # Executed steps are keyed by step_id only. Legacy multiple-based keys
        # are migrated to step_id when seen (backward compatibility).
        key = str(step.step_id)
        if key in mint_state.executed_steps:
            continue
        legacy_key = f"{step.multiple:.2f}"
        if legacy_key in mint_state.executed_steps:
            mint_state.executed_steps[key] = mint_state.executed_steps[legacy_key]
            continue
        return step, key
    return None


def _update_failures_on_error(mint_state, error: Exception, config: Config, mint: Optional[str] = None) -> None:
    from .events import MINT_PAUSED
    failures = mint_state.failures
    failures.count += 1
    failures.last_error = str(error)
    if failures.count >= config.max_consecutive_failures:
        pause_min = getattr(config, "fail_pause_minutes", 15) or 15  # CEO directive §9: 15 min
        failures.paused_until = datetime.now(tz=timezone.utc) + timedelta(minutes=pause_min)
        logger.warning(
            "%s mint=%s consecutive_failures=%d paused_minutes=%d",
            MINT_PAUSED,
            (mint or "?")[:12],
            failures.count,
            pause_min,
        )


def _reset_failures(mint_state) -> None:
    failures = mint_state.failures
    failures.count = 0
    failures.last_error = None
    failures.paused_until = None


# --- T41: wallet buy detection and dynamic lots ---

def _trading_bag_from_lots(mint_state: RuntimeMintState) -> int:
    """
    Sum remaining_amount of active tx-derived lots with valid entry.

    Only lots that are both:
    - active, and
    - tx-derived / intentional trading sources, and
    - have a valid entry_price_sol_per_token
    contribute to the tradable bag. Snapshot / unknown-entry / bootstrap-only
    inventory is excluded from trading_bag_raw but still visible in reconciliation.
    """
    lots = getattr(mint_state, "lots", None) or []
    total = 0
    for lot in lots:
        status = getattr(lot, "status", "active")
        if status != "active":
            continue
        src = getattr(lot, "source", "")
        # Only tx-derived / intentional trading sources are eligible for bag.
        if src not in ("tx_exact", "tx_parsed", "buyback", "bootstrap_buy"):
            continue
        ep = getattr(lot, "entry_price_sol_per_token", None)
        if ep is None:
            continue
        try:
            ep_f = float(ep)
        except (TypeError, ValueError):
            continue
        if not validate_entry_price(ep_f):
            continue
        try:
            total += int(getattr(lot, "remaining_amount", 0) or 0)
        except (ValueError, TypeError):
            pass
    return total


def compute_manual_override_tradable_raw(
    mint_state: RuntimeMintState,
    config: Config,
    mint_addr: Optional[str] = None,
) -> int:
    """
    Compute tradable amount from manual override inventory for a mint.

    Returns 0 unless:
    - enable_manual_override_inventory is true
    - mint is explicitly allow-listed
    - record.operator_approved is true
    """
    try:
        if not getattr(config, "enable_manual_override_inventory", False):
            return 0
    except Exception:
        return 0

    allowed = set(getattr(config, "manual_override_allowed_mints", []) or [])
    if mint_addr is not None and mint_addr not in allowed:
        return 0
    if mint_addr is None and not allowed:
        return 0

    total = 0
    for rec in getattr(mint_state, "manual_override_inventory", None) or []:
        try:
            approved = getattr(rec, "operator_approved", False)
            if not approved:
                continue
            amt = int(getattr(rec, "amount_raw", 0) or 0)
        except (ValueError, TypeError):
            continue
        if amt > 0:
            total += amt
    if total < 0:
        total = 0
    return total


def _estimate_wallet_balance_raw(mint_state: RuntimeMintState) -> int:
    """
    Best-effort estimate of wallet balance for this mint from state.
    Uses last_known_balance_raw when available; otherwise falls back to lot sum.
    """
    try:
        if getattr(mint_state, "last_known_balance_raw", None) is not None:
            return int(mint_state.last_known_balance_raw or 0)
    except (ValueError, TypeError):
        pass
    try:
        return _trading_bag_from_lots(mint_state)
    except Exception:
        return 0


def _update_trading_bag_with_override(
    mint_state: RuntimeMintState,
    config: Config,
    mint_addr: str,
    wallet_balance_raw: Optional[int] = None,
) -> None:
    """
    Update trading_bag_raw and manual_override_tradable_raw from tx-proven lots + manual override pool.

    Invariants:
    - tx-proven bag (from lots) is unchanged.
    - manual override pool contributes only when enabled + allow-listed.
    - combined bag never exceeds wallet_balance_raw estimate.
    """
    tx_bag = _trading_bag_from_lots(mint_state)
    override_bag = compute_manual_override_tradable_raw(mint_state, config, mint_addr)
    if wallet_balance_raw is None:
        wallet_balance_raw = _estimate_wallet_balance_raw(mint_state)
    if wallet_balance_raw is None:
        wallet_balance_raw = 0
    try:
        wb = int(wallet_balance_raw)
    except (ValueError, TypeError):
        wb = 0
    capacity_for_override = max(0, wb - tx_bag)
    applied_override = min(override_bag, capacity_for_override)
    combined_bag = tx_bag + applied_override
    if combined_bag < 0:
        combined_bag = 0
    mint_state.trading_bag_raw = str(combined_bag)
    mint_state.manual_override_tradable_raw = str(applied_override)


def _evaluate_manual_override_bypass(
    mint: str,
    mint_state: RuntimeMintState,
    actual_raw: int,
    config: Config,
    event_journal_path: Optional[Path],
) -> int:
    """
    Evaluate manual override reconciliation bypass policy for one mint.

    Returns current manual_override_tradable_raw (effective override bag) after
    applying policy, without changing trading_bag_raw. Caller decides how to use
    the value for effective tradable bag.
    """
    override_tradable = 0
    try:
        override_tradable = compute_manual_override_tradable_raw(mint_state, config, mint_addr=mint)
    except Exception:
        override_tradable = 0

    bypass_enabled = getattr(config, "manual_override_bypass_enabled", False)
    allowed = set(getattr(config, "manual_override_bypass_allowed_mints", []) or [])
    require_approval = getattr(config, "manual_override_bypass_require_operator_approval", True)
    min_override = getattr(config, "manual_override_bypass_min_override_raw", 0) or 0

    # Require global enable, per-mint allow-list, and non-zero override that meets threshold.
    eligible = (
        bypass_enabled
        and mint in allowed
        and override_tradable >= min_override
    )

    # Require operator-approved records when configured (compute_manual_override_tradable_raw
    # already filters to operator_approved, but we keep the flag for clarity).
    if require_approval and override_tradable <= 0:
        eligible = False

    # Bypass should only be active during reconciliation-based pause.
    failures = getattr(mint_state, "failures", None)
    paused_for_recon = False
    if failures is not None and getattr(failures, "paused_until", None) is not None:
        if getattr(failures, "last_error", "") == "reconciliation_mismatch":
            paused_for_recon = True

    should_be_active = bool(eligible and paused_for_recon)
    was_active = getattr(mint_state, "manual_override_bypass_active", False)

    if should_be_active and not was_active:
        mint_state.manual_override_bypass_active = True
        mint_state.manual_override_bypass_reason = "manual_override_reconciliation_bypass_v1"
        if event_journal_path:
            try:
                append_event(
                    event_journal_path,
                    MANUAL_OVERRIDE_BYPASS_ENABLED,
                    {
                        "mint": mint[:12],
                        "policy": "manual_override_reconciliation_bypass",
                        "override_amount_raw": override_tradable,
                    },
                )
            except Exception:
                pass
    elif not should_be_active and was_active:
        mint_state.manual_override_bypass_active = False
        mint_state.manual_override_bypass_reason = None
        if event_journal_path:
            try:
                append_event(
                    event_journal_path,
                    MANUAL_OVERRIDE_BYPASS_DISABLED,
                    {
                        "mint": mint[:12],
                        "policy": "manual_override_reconciliation_bypass",
                    },
                )
            except Exception:
                pass

    # Clamp override to wallet balance when positive.
    effective_override = 0
    if override_tradable > 0 and actual_raw > 0:
        try:
            effective_override = min(override_tradable, int(actual_raw))
        except (TypeError, ValueError):
            effective_override = 0

    mint_state.manual_override_tradable_raw = str(effective_override) if effective_override > 0 else "0"
    return effective_override


def _apply_sell_inventory_effects(
    mint_state: RuntimeMintState,
    config: Config,
    mint_addr: str,
    sold_raw: int,
    journal_path: Optional[Path],
    tx_signature: Optional[str],
) -> None:
    """
    Apply inventory effects for a completed sell:
    - Consume tx-proven lots first (FIFO).
    - Spill remainder into manual override inventory.
    - Recompute trading bag with override contribution, capped by wallet balance.
    """
    if sold_raw <= 0:
        return
    # Tx-proven lots first.
    tx_bag_before = _trading_bag_from_lots(mint_state)
    from_tx = min(sold_raw, tx_bag_before)
    if from_tx > 0:
        _debit_lots_fifo(mint_state, from_tx)
    # Manual override spill (may be zero when tx lots fully cover sell).
    override_to_consume = sold_raw - from_tx
    if override_to_consume > 0:
        _consume_manual_override(
            mint_state,
            override_to_consume,
            journal_path,
            mint_addr,
            tx_signature,
        )
    # Recompute trading bag using tx lots + approved override, capped by wallet balance estimate.
    wallet_raw_est = _estimate_wallet_balance_raw(mint_state)
    _update_trading_bag_with_override(mint_state, config, mint_addr, wallet_raw_est)


def _get_sold_bot_and_external_from_steps(mint_state: RuntimeMintState) -> Tuple[int, int]:
    """Compute (sold_bot_raw, sold_external_raw) from executed_steps. ext_* keys = external."""
    bot_raw = 0
    ext_raw = 0
    for step_key, step_info in (getattr(mint_state, "executed_steps", None) or {}).items():
        try:
            amt = int(getattr(step_info, "sold_raw", 0) or 0)
        except (ValueError, TypeError):
            amt = 0
        if isinstance(step_key, str) and step_key.startswith("ext_"):
            ext_raw += amt
        else:
            bot_raw += amt
    return bot_raw, ext_raw


def _ensure_sell_accounting_backfill(mint_state: RuntimeMintState) -> None:
    """If sold_bot_raw/sold_external_raw are missing, populate from executed_steps (migration/legacy)."""
    if getattr(mint_state, "sold_bot_raw", None) is not None and getattr(mint_state, "sold_external_raw", None) is not None:
        return
    bot_raw, ext_raw = _get_sold_bot_and_external_from_steps(mint_state)
    mint_state.sold_bot_raw = str(bot_raw)
    mint_state.sold_external_raw = str(ext_raw)


def _add_sell_accounting(
    mint_state: RuntimeMintState,
    bot_delta: int = 0,
    external_delta: int = 0,
    journal_path: Optional[Path] = None,
    mint: str = "",
    step_key: str = "",
    tx_sig: Optional[str] = None,
    sold_raw: int = 0,
    source: Literal["bot", "external"] = "bot",
) -> None:
    """
    Sync sold_bot_raw and sold_external_raw from executed_steps and assert invariant.

    Callers are expected to update mint_state.executed_steps[step_key] before invoking
    this helper. bot_delta / external_delta are kept for backward compatibility but
    the source of truth is executed_steps, not the deltas.
    """
    bot_raw, ext_raw = _get_sold_bot_and_external_from_steps(mint_state)
    mint_state.sold_bot_raw = str(bot_raw)
    mint_state.sold_external_raw = str(ext_raw)
    sum_from_steps = bot_raw + ext_raw
    # Invariant is enforced by construction; retain logging for defensive checks.
    if sum_from_steps != int(mint_state.sold_bot_raw or 0) + int(mint_state.sold_external_raw or 0):
        logger.error(
            "SELL_ACCOUNTING_INVARIANT_BROKEN mint=%s sold_bot_raw=%s sold_external_raw=%s sum_steps=%s",
            mint[:12] if mint else "?",
            mint_state.sold_bot_raw,
            mint_state.sold_external_raw,
            sum_from_steps,
        )
        if journal_path:
            try:
                append_event(
                    journal_path,
                    EVENT_SELL_ACCOUNTING_INVARIANT_BROKEN,
                    {
                        "mint": mint[:12] if mint else None,
                        "sold_bot_raw": mint_state.sold_bot_raw,
                        "sold_external_raw": mint_state.sold_external_raw,
                        "sum_from_steps": sum_from_steps,
                    },
                )
            except Exception:
                pass
    if journal_path and sold_raw and source == "bot":
        try:
            append_event(
                journal_path,
                EVENT_BOT_SELL_ACCOUNTED,
                {"mint": mint[:12] if mint else None, "step_id": step_key, "sold_raw": sold_raw, "source": "bot", "tx_sig": (tx_sig[:16] if tx_sig else None)},
            )
        except Exception:
            pass
    if journal_path and sold_raw and source == "external":
        try:
            append_event(
                journal_path,
                EVENT_EXTERNAL_SELL_ACCOUNTED,
                {"mint": mint[:12] if mint else None, "sold_raw": sold_raw, "source": "external", "tx_sig": (tx_sig[:16] if tx_sig else None)},
            )
        except Exception:
            pass


def _compute_mint_holding_explanation(mint_state: RuntimeMintState) -> Dict[str, int]:
    """
    Reconciliation model: explain holdings by source.
    Returns dict: tx_derived_raw, bootstrap_snapshot_raw, transfer_unknown_raw, sold_raw, sum_active_lots.
    sold_raw uses sold_bot_raw + sold_external_raw when set; else sum(executed_steps).
    """
    lots = getattr(mint_state, "lots", None) or []
    tx_derived_raw = 0
    bootstrap_snapshot_raw = 0
    transfer_unknown_raw = 0
    for lot in lots:
        status = getattr(lot, "status", "active")
        if status != "active":
            continue
        try:
            rem = int(getattr(lot, "remaining_amount", 0) or 0)
        except (ValueError, TypeError):
            rem = 0
        src = getattr(lot, "source", "")
        if src in ("tx_exact", "tx_parsed"):
            tx_derived_raw += rem
        elif src == "bootstrap_snapshot" or src in ("initial_migration", "snapshot"):
            bootstrap_snapshot_raw += rem
        else:
            transfer_unknown_raw += rem
    sold_raw = 0
    if getattr(mint_state, "sold_bot_raw", None) is not None and getattr(mint_state, "sold_external_raw", None) is not None:
        try:
            sold_raw = int(mint_state.sold_bot_raw or 0) + int(mint_state.sold_external_raw or 0)
        except (ValueError, TypeError):
            pass
    if sold_raw == 0:
        for step_info in (getattr(mint_state, "executed_steps", None) or {}).values():
            try:
                sold_raw += int(getattr(step_info, "sold_raw", 0) or 0)
            except (ValueError, TypeError):
                pass
    sum_active_lots = tx_derived_raw + bootstrap_snapshot_raw + transfer_unknown_raw
    return {
        "tx_derived_raw": tx_derived_raw,
        "bootstrap_snapshot_raw": bootstrap_snapshot_raw,
        "transfer_unknown_raw": transfer_unknown_raw,
        "sold_raw": sold_raw,
        "sum_active_lots": sum_active_lots,
    }


def _delta_explained_by_existing_tx_exact_lots(mint_state: RuntimeMintState, delta_raw: int) -> bool:
    """
    Return True if delta_raw is exactly the sum of some subset of existing tx_exact lots
    for this mint (within 1% tolerance). Used to avoid creating duplicate fallback lots.
    """
    if delta_raw <= 0:
        return False
    lots = getattr(mint_state, "lots", None) or []
    tx_exact_amounts: List[int] = []
    for lot in lots:
        if getattr(lot, "source", "") != "tx_exact":
            continue
        if getattr(lot, "status", "active") != "active":
            continue
        try:
            tx_exact_amounts.append(int(getattr(lot, "token_amount", 0) or 0))
        except (ValueError, TypeError):
            pass
    if not tx_exact_amounts:
        return False
    tolerance = max(1, int(delta_raw * 0.01))
    # Subset-sum: try all subsets (n small in practice)
    for r in range(1, len(tx_exact_amounts) + 1):
        for subset in itertools.combinations(tx_exact_amounts, r):
            if abs(sum(subset) - delta_raw) <= tolerance:
                return True
    return False


def _debit_lots_fifo(mint_state: RuntimeMintState, amount_raw: int) -> None:
    """Reduce lot remaining_amounts FIFO by amount_raw. Mark lots fully_sold when exhausted."""
    lots = getattr(mint_state, "lots", None) or []
    if not lots:
        return
    remaining_to_debit = amount_raw
    for lot in lots:
        if remaining_to_debit <= 0 or getattr(lot, "status", "active") != "active":
            continue
        rem = int(lot.remaining_amount)
        if rem <= 0:
            lot.status = "fully_sold"
            continue
        take = min(rem, remaining_to_debit)
        lot.remaining_amount = str(rem - take)
        remaining_to_debit -= take
        if int(lot.remaining_amount) <= 0:
            lot.status = "fully_sold"
        if remaining_to_debit <= 0:
            break


def _ingest_external_sells(
    state: RuntimeState,
    rpc: RpcClient,
    wallet: str,
    max_signatures: int = 200,
    journal_path: Optional[Path] = None,
    config: Optional[Config] = None,
) -> int:
    """
    Scan recent wallet txs, detect sells (token out + SOL in) for tracked mints,
    and ingest into executed_steps + debit lots FIFO. Idempotent: skips sig already in executed_steps.
    Returns number of external sells ingested.
    """
    from .rpc import RpcError

    mints_tracked = set(state.mints.keys())
    if not mints_tracked:
        return 0
    existing_sigs_per_mint: Dict[str, Set[str]] = {}
    for mint, ms in state.mints.items():
        sigs = set()
        for step in (getattr(ms, "executed_steps", None) or {}).values():
            if getattr(step, "sig", None):
                sigs.add(step.sig)
        existing_sigs_per_mint[mint] = sigs
    # Paginate: Solana returns max 1000 per request
    sig_list: List[Dict[str, Any]] = []
    page_limit = 1000
    before: Optional[str] = None
    try:
        while len(sig_list) < max_signatures:
            fetch_limit = min(page_limit, max_signatures - len(sig_list))
            batch = rpc.get_signatures_for_address(wallet, limit=fetch_limit, before=before)
            if not batch:
                break
            sig_list.extend(batch)
            if len(batch) < fetch_limit:
                break
            before = batch[-1].get("signature") if isinstance(batch[-1], dict) else None
            if not before:
                break
    except Exception as exc:
        logger.warning("EXTERNAL_SELL_INGEST get_signatures_for_address failed: %s", exc)
        return 0
    ingested = 0
    failures = 0
    for sig_info in sig_list:
        signature = sig_info.get("signature") if isinstance(sig_info, dict) else None
        if not signature:
            continue
        try:
            tx = rpc.get_transaction(signature)
        except Exception as exc:
            failures += 1
            if failures >= 5 or isinstance(exc, RpcError):
                break
            continue
        if not tx:
            continue
        events = parse_sell_events_from_tx(tx, wallet, mints_tracked, signature)
        if not events:
            token_delta_raw = 0
            sol_delta = 0.0
            logger.info(
                "EXTERNAL_TX_OBSERVED sig=%s mint=%s token_delta_raw=%s sol_delta=%.6f classification=%s",
                signature[:16],
                "",
                token_delta_raw,
                sol_delta,
                "unknown",
            )
            if journal_path:
                try:
                    append_event(
                        journal_path,
                        EXTERNAL_TX_OBSERVED,
                        {
                            "signature": signature[:16],
                            "mint": "",
                            "token_delta_raw": token_delta_raw,
                            "sol_delta": sol_delta,
                            "classification": "unknown",
                        },
                    )
                    append_event(
                        journal_path,
                        UNCLASSIFIED_EXTERNAL_TX,
                        {
                            "signature": signature[:16],
                            "mint": "",
                            "token_delta_raw": token_delta_raw,
                            "sol_delta": sol_delta,
                            "reason": "parse_sell_events_returned_empty",
                        },
                    )
                except Exception:
                    pass
            logger.warning(
                "UNCLASSIFIED_EXTERNAL_TX sig=%s mint=%s token_delta_raw=%s sol_delta=%.6f reason=%s",
                signature[:16],
                "",
                token_delta_raw,
                sol_delta,
                "parse_sell_events_returned_empty",
            )
            continue
        for ev in events:
            ms = state.mints.get(ev.mint)
            if not ms:
                continue
            if signature in existing_sigs_per_mint.get(ev.mint, set()):
                continue
            step_key = "ext_" + signature[:12].replace("/", "_")
            executed_info = StepExecutionInfo(
                sig=signature,
                time=ev.block_time or datetime.now(tz=timezone.utc),
                sold_raw=str(ev.sold_raw),
                sol_out=ev.sol_in_lamports / 1e9,
            )
            ms.executed_steps = getattr(ms, "executed_steps", None) or {}
            ms.executed_steps[step_key] = executed_info
            _add_sell_accounting(
                ms,
                external_delta=ev.sold_raw,
                journal_path=journal_path,
                mint=ev.mint,
                step_key=step_key,
                tx_sig=signature,
                sold_raw=ev.sold_raw,
                source="external",
            )
            existing_sigs_per_mint.setdefault(ev.mint, set()).add(signature)
            # Use provided config when available; fall back to a fresh Config()
            # so external-sell ingest remains idempotent for backfill/offline tools.
            _apply_sell_inventory_effects(
                mint_state=ms,
                config=config or Config(),
                mint_addr=ev.mint,
                sold_raw=ev.sold_raw,
                journal_path=journal_path,
                tx_signature=signature,
            )
            ingested += 1
            logger.info(
                "EXTERNAL_TX_OBSERVED sig=%s mint=%s token_delta_raw=%s sol_delta=%.6f classification=%s",
                signature[:16],
                ev.mint[:12],
                ev.sold_raw,
                ev.sol_in_lamports / 1e9,
                "external_sell",
            )
            if journal_path:
                try:
                    append_event(
                        journal_path,
                        EXTERNAL_TX_OBSERVED,
                        {
                            "signature": signature[:16],
                            "mint": ev.mint[:12],
                            "token_delta_raw": ev.sold_raw,
                            "sol_delta": ev.sol_in_lamports / 1e9,
                            "classification": "external_sell",
                        },
                    )
                except Exception:
                    pass
            logger.info(
                "EXTERNAL_SELL_INGESTED mint=%s sig=%s sold_raw=%s sol_out=%.6f",
                ev.mint[:12], signature[:16], ev.sold_raw, ev.sol_in_lamports / 1e9,
            )
            if journal_path:
                try:
                    append_event(journal_path, "EXTERNAL_SELL_INGESTED", {"mint": ev.mint[:12], "tx_sig": signature[:16], "sold_raw": ev.sold_raw})
                except Exception:
                    pass
    return ingested


def _ingest_external_sells_from_sig_list(
    state: RuntimeState,
    rpc: RpcClient,
    wallet: str,
    sorted_sig_list: List[Dict[str, Any]],
    journal_path: Optional[Path] = None,
) -> int:
    """
    Same as _ingest_external_sells but uses a pre-built sorted signature list
    (e.g. merged wallet + token-account history, oldest-first). Used for full-history scratch rebuild.
    """
    from .rpc import RpcError

    mints_tracked = set(state.mints.keys())
    if not mints_tracked:
        return 0
    existing_sigs_per_mint: Dict[str, Set[str]] = {}
    for mint, ms in state.mints.items():
        sigs = set()
        for step in (getattr(ms, "executed_steps", None) or {}).values():
            if getattr(step, "sig", None):
                sigs.add(step.sig)
        existing_sigs_per_mint[mint] = sigs
    sig_list = sorted_sig_list
    ingested = 0
    failures = 0
    for sig_info in sig_list:
        signature = sig_info.get("signature") if isinstance(sig_info, dict) else None
        if not signature:
            continue
        try:
            tx = rpc.get_transaction(signature)
        except Exception as exc:
            failures += 1
            if failures >= 5 or isinstance(exc, RpcError):
                break
            continue
        if not tx:
            continue
        events = parse_sell_events_from_tx(tx, wallet, mints_tracked, signature)
        for ev in events:
            ms = state.mints.get(ev.mint)
            if not ms:
                continue
            if signature in existing_sigs_per_mint.get(ev.mint, set()):
                continue
            step_key = "ext_" + signature[:12].replace("/", "_")
            executed_info = StepExecutionInfo(
                sig=signature,
                time=ev.block_time or datetime.now(tz=timezone.utc),
                sold_raw=str(ev.sold_raw),
                sol_out=ev.sol_in_lamports / 1e9,
            )
            ms.executed_steps = getattr(ms, "executed_steps", None) or {}
            ms.executed_steps[step_key] = executed_info
            _add_sell_accounting(
                ms,
                external_delta=ev.sold_raw,
                journal_path=journal_path,
                mint=ev.mint,
                step_key=step_key,
                tx_sig=signature,
                sold_raw=ev.sold_raw,
                source="external",
            )
            existing_sigs_per_mint.setdefault(ev.mint, set()).add(signature)
            _apply_sell_inventory_effects(
                mint_state=ms,
                config=config,
                mint_addr=ev.mint,
                sold_raw=ev.sold_raw,
                journal_path=journal_path,
                tx_signature=signature,
            )
            ingested += 1
            logger.info(
                "EXTERNAL_SELL_INGESTED mint=%s sig=%s sold_raw=%s sol_out=%.6f",
                ev.mint[:12], signature[:16], ev.sold_raw, ev.sol_in_lamports / 1e9,
            )
            if journal_path:
                try:
                    append_event(journal_path, "EXTERNAL_SELL_INGESTED", {"mint": ev.mint[:12], "tx_sig": signature[:16], "sold_raw": ev.sold_raw})
                except Exception:
                    pass
    return ingested


def _ensure_lots_migrated(state: RuntimeState) -> None:
    """If a mint has no lots, create one synthetic lot from current trading_bag_raw (bootstrap_snapshot only; excluded from trading bag until confirmed)."""
    for mint, ms in state.mints.items():
        lots = getattr(ms, "lots", None) or []
        if lots:
            continue
        try:
            bag = int(ms.trading_bag_raw)
        except (ValueError, TypeError):
            continue
        if bag <= 0:
            continue
        confidence: str = "unknown"
        if getattr(ms, "entry_source", None) in ("user", "inferred_from_tx", "bootstrap_buy", "market_bootstrap"):
            confidence = "known" if ms.entry_source == "user" else "inferred"
        entry = getattr(ms, "entry_price_sol_per_token", None) or 0.0
        synthetic = LotInfo.create(
            mint=mint,
            token_amount_raw=bag,
            entry_price=entry if entry > 0 else None,
            confidence=confidence,  # type: ignore[arg-type]
            source="bootstrap_snapshot",
            entry_confidence="snapshot",
        )
        ms.lots = [synthetic]
        logger.info(
            "LOT_SOURCE_BOOTSTRAP mint=%s lot_id=%s source=bootstrap_snapshot remaining=%s reason=no_tx_derived_lots_after_tx_first",
            mint[:12], synthetic.lot_id[:8], synthetic.remaining_amount,
        )


def _run_buy_detection(
    state: RuntimeState,
    balances_by_mint: Dict[str, int],
    config: Config,
    rpc: Optional[RpcClient] = None,
    safety_path: Optional[Path] = None,
    journal_path: Optional[Path] = None,
    wallet_pubkey: Optional[str] = None,
    project_root: Optional[Path] = None,
    symbol_by_mint: Optional[Dict[str, str]] = None,
    decimals_by_mint: Optional[Dict[str, int]] = None,
) -> None:
    """
    Balance-delta reconciliation only. Lot creation is tx-only: create lots only when a matching
    parsed transaction is found (tx_exact). If balance increases but no tx is found, log
    BALANCE_DELTA_WITHOUT_TX and do not create a lot. Balance deltas are used for integrity
    checks, alerting, and reconciliation warnings only.
    """
    threshold = getattr(config, "min_buy_detection_raw", 10_000) or 10_000
    lot_mode = (getattr(config, "lot_mode", "new_lot_per_buy") or "new_lot_per_buy").strip().lower()
    if lot_mode not in ("new_lot_per_buy", "aggregate_position"):
        lot_mode = "new_lot_per_buy"
    max_fp = getattr(config, "max_processed_fingerprints", 5000) or 5000
    slot = 0
    if rpc and safety_path:
        try:
            slot = rpc.get_slot()
        except Exception:
            pass
    for mint, current_raw in balances_by_mint.items():
        ms = state.mints.get(mint)
        if ms is None:
            continue
        prior_str = getattr(ms, "last_known_balance_raw", None)
        if prior_str is None:
            ms.last_known_balance_raw = str(current_raw)
            continue
        try:
            prior = int(prior_str)
        except (ValueError, TypeError):
            ms.last_known_balance_raw = str(current_raw)
            continue
        if current_raw < prior:
            ms.last_known_balance_raw = str(current_raw)
            lots = getattr(ms, "lots", None) or []
            if lots:
                total_from_lots = _trading_bag_from_lots(ms)
                if total_from_lots > current_raw:
                    trading_bag_raw, moonbag_raw = compute_trading_bag(
                        balance_raw=str(current_raw),
                        trading_bag_pct=config.trading_bag_pct,
                    )
                    ms.trading_bag_raw = str(trading_bag_raw)
                    ms.moonbag_raw = str(moonbag_raw)
                    entry_val = getattr(ms, "entry_price_sol_per_token", None) or 0.0
                    conf: str = "inferred" if getattr(ms, "entry_source", None) in ("market_bootstrap", "bootstrap_buy", "inferred_from_tx") else "unknown"
                    if getattr(ms, "entry_source", None) == "user":
                        conf = "known"
                    resync_lot = LotInfo.create(
                        mint=mint,
                        token_amount_raw=trading_bag_raw,
                        entry_price=entry_val if entry_val > 0 else None,
                        confidence=conf,  # type: ignore[arg-type]
                        source="bootstrap_snapshot",
                        entry_confidence="snapshot",
                    )
                    ms.lots = [resync_lot]
                    logger.info("LOT_RESYNC mint=%s balance_dropped source=bootstrap_snapshot remaining=%s", mint[:12], trading_bag_raw)
            continue
        # Reconciliation-first: compare current balance to sum(lots). Tx-first engine already created lots from txs.
        sum_lots = _trading_bag_from_lots(ms)
        if current_raw <= sum_lots:
            ms.last_known_balance_raw = str(current_raw)
            lots = getattr(ms, "lots", None) or []
            if lots:
                ms.trading_bag_raw = str(_trading_bag_from_lots(ms))
            continue
        unmatched_raw = current_raw - sum_lots
        if unmatched_raw < threshold:
            ms.last_known_balance_raw = str(current_raw)
            lots = getattr(ms, "lots", None) or []
            if lots:
                ms.trading_bag_raw = str(_trading_bag_from_lots(ms))
            continue
        unresolved_fp = make_unresolved_delta_fingerprint(mint, unmatched_raw)
        fingerprint = make_fingerprint(mint, unmatched_raw, slot)
        if safety_path and is_duplicate_fingerprint(safety_path, fingerprint, max_fp):
            logger.debug("BUY_DETECTION_SKIPPED mint=%s reason=duplicate_fingerprint fingerprint=%s", mint[:12], fingerprint)
            ms.last_known_balance_raw = str(current_raw)
            lots = getattr(ms, "lots", None) or []
            if lots:
                ms.trading_bag_raw = str(_trading_bag_from_lots(ms))
            continue
        # Dedup: already reported this (mint, delta) as unresolved — do not re-run tx lookup or re-emit events every cycle.
        unresolved_fp = make_unresolved_delta_fingerprint(mint, unmatched_raw)
        if safety_path and is_duplicate_fingerprint(safety_path, unresolved_fp, max_fp):
            logger.debug("UNRESOLVED_DELTA_ALREADY_REPORTED mint=%s unmatched_raw=%s (informational only; no lot)", mint[:12], unmatched_raw)
            ms.last_known_balance_raw = str(current_raw)
            lots = getattr(ms, "lots", None) or []
            if lots:
                ms.trading_bag_raw = str(_trading_bag_from_lots(ms))
            continue
        # Balance-delta lot creation disabled: only parsed transactions create lots. Do not aggregate into existing lot from delta.
        if lot_mode == "aggregate_position":
            ms.last_known_balance_raw = str(current_raw)
            logger.warning("BALANCE_DELTA_WITHOUT_TX mint=%s unmatched_raw=%s (aggregate mode; no lot created — tx-only policy)", mint[:12], unmatched_raw)
            if journal_path:
                append_event(journal_path, "BALANCE_DELTA_WITHOUT_TX", {"mint": mint[:12], "unmatched_raw": unmatched_raw, "reason": "tx_only_policy"})
            continue
        # Guard: delta already explained by existing tx_exact lots (multi-tx duplicate prevention).
        if _delta_explained_by_existing_tx_exact_lots(ms, unmatched_raw):
            logger.info(
                "DELTA_ALREADY_EXPLAINED_BY_EXISTING_LOTS mint=%s unmatched_raw=%s DUPLICATE_FALLBACK_LOT_SKIPPED",
                mint[:12], unmatched_raw,
            )
            if journal_path:
                from .events import EVENT_DUPLICATE_FALLBACK_LOT_SKIPPED
                append_event(journal_path, "DELTA_ALREADY_EXPLAINED_BY_EXISTING_LOTS", {"mint": mint[:12], "unmatched_raw": unmatched_raw})
                append_event(journal_path, EVENT_DUPLICATE_FALLBACK_LOT_SKIPPED, {"mint": mint[:12], "unmatched_raw": unmatched_raw})
            ms.last_known_balance_raw = str(current_raw)
            lots = getattr(ms, "lots", None) or []
            if lots:
                ms.trading_bag_raw = str(_trading_bag_from_lots(ms))
            continue
        # Unmatched balance increase: try to find tx for this delta (fallback when tx-first missed it, e.g. beyond sig limit).
        entry: float = 0.0
        confidence: str = "inferred"
        if getattr(ms, "entry_source", None) == "user":
            confidence = "known"
        elif getattr(ms, "entry_source", None) in ("market_bootstrap", "bootstrap_buy", "inferred_from_tx"):
            confidence = "inferred"
        else:
            confidence = "unknown"
        lot_source = "wallet_buy_detected"
        lot_entry_confidence: str = "pending_price_resolution"
        lot_confidence = confidence
        existing_sigs: Set[str] = set()
        for l in getattr(ms, "lots", None) or []:
            s = getattr(l, "tx_signature", None)
            if s:
                existing_sigs.add(s)
        tx_sig: Optional[str] = None
        tx_price: Optional[float] = None
        tx_detected_at: Optional[datetime] = None
        if rpc and wallet_pubkey and unmatched_raw > 0:
            from .tx_infer import find_buy_tx_for_delta
            max_sigs = min(max(20, getattr(config, "entry_infer_signature_limit", 60) or 60), ENTRY_SCAN_MAX_SIGNATURES)
            dec = (decimals_by_mint or {}).get(mint, 6)
            lookup_reason_out: List[str] = []
            buy_tx = find_buy_tx_for_delta(
                wallet_pubkey, mint, unmatched_raw, rpc,
                max_signatures=max_sigs, exclude_signatures=existing_sigs, decimals=dec,
                failure_reason_out=lookup_reason_out,
            )
            if buy_tx:
                tx_sig, tx_price, tx_detected_at = buy_tx
                if tx_price is not None and validate_entry_price(tx_price):
                    lot_source = "tx_exact"
                    lot_entry_confidence = "exact"
                    lot_confidence = "known"
                    entry = tx_price
                else:
                    if tx_price is not None and journal_path:
                        append_event(journal_path, "PRICE_SANITY_REJECTED", {"mint": mint[:12], "tx_sig": (tx_sig[:16] if tx_sig else None), "price": tx_price, "reason": "outside ENTRY_PRICE bounds"})
                    logger.warning("PRICE_SANITY_REJECTED mint=%s tx_price=%.6e outside bounds; entry=null confidence=unknown", mint[:12], tx_price or 0)
                    tx_sig = None
                    tx_price = None
                    tx_detected_at = None
            else:
                # Try multi-tx match (set of txs that sum to delta).
                from .tx_infer import find_buy_txs_for_delta_sum
                buy_txs = find_buy_txs_for_delta_sum(
                    wallet_pubkey, mint, unmatched_raw, rpc,
                    max_signatures=max_sigs, exclude_signatures=existing_sigs, decimals=dec,
                )
                if buy_txs:
                    # Create one tx_exact lot per matched tx (multi-tx delta matched).
                    from .tx_infer import _parse_token_deltas_for_mints
                    created_any = False
                    for sig, price, detected_at in buy_txs:
                        if not validate_entry_price(price):
                            continue
                        amt = 0
                        try:
                            tx = rpc.get_transaction(sig)
                            if tx:
                                deltas = _parse_token_deltas_for_mints(tx, wallet_pubkey, [mint])
                                amt = deltas.get(mint, 0)
                        except Exception:
                            amt = unmatched_raw // len(buy_txs) if len(buy_txs) > 0 else 0
                        if amt <= 0:
                            continue
                        sub_lot = LotInfo.create(
                            mint=mint,
                            token_amount_raw=amt,
                            entry_price=price,
                            confidence="known",
                            source="tx_exact",
                            entry_confidence="exact",
                            tx_signature=sig,
                            detected_at=detected_at,
                        )
                        ms.lots = getattr(ms, "lots", None) or []
                        ms.lots.append(sub_lot)
                        existing_sigs.add(sig)
                        created_any = True
                        if journal_path:
                            append_event(journal_path, EVENT_LOT_CREATED_TX_EXACT, {"mint": mint[:12], "lot_id": sub_lot.lot_id[:8], "tx_signature": sig[:16], "entry_price": price})
                        logger.info("MULTI_TX_DELTA_MATCHED mint=%s lot_id=%s sig=%s price=%.6e amount_raw=%s", mint[:12], sub_lot.lot_id[:8], sig[:16], price, amt)
                    if created_any:
                        if safety_path:
                            add_processed_fingerprint(safety_path, fingerprint, max_fp)
                        ms.last_known_balance_raw = str(current_raw)
                        ms.trading_bag_raw = str(_trading_bag_from_lots(ms))
                        if journal_path:
                            append_event(journal_path, EVENT_LOT_CREATED, {"mint": mint[:12], "lot_id": "multi", "token_amount_raw": unmatched_raw, "entry_confidence": "exact", "source": "tx_exact"})
                        logger.info("BUY_DETECTED mint=%s unmatched_raw=%s source=tx_exact multi_tx_lots=%s", mint[:12], unmatched_raw, sum(1 for _ in buy_txs))
                        continue
                # Single event per (mint, delta); no tradable lot; policy: unresolved_informational_only.
                lookup_reason = (lookup_reason_out[0] if lookup_reason_out else "no_matching_tx")
                logger.warning("BALANCE_DELTA_WITHOUT_TX mint=%s unmatched_raw=%s (no matching tx; no lot created — tx-only policy) lookup_reason=%s", mint[:12], unmatched_raw, lookup_reason)
                if journal_path:
                    append_event(
                        journal_path,
                        EVENT_UNRESOLVED_BALANCE_DELTA,
                        {
                            "mint": mint[:12],
                            "unmatched_raw": unmatched_raw,
                            "reason": "no_matching_tx",
                            "lookup_reason": lookup_reason,
                            "note": "informational_only_no_lot",
                        },
                    )
                if safety_path:
                    add_processed_fingerprint(safety_path, unresolved_fp, max_fp)
                ms.last_known_balance_raw = str(current_raw)
                lots = getattr(ms, "lots", None) or []
                if lots:
                    ms.trading_bag_raw = str(_trading_bag_from_lots(ms))
                continue
        if tx_price is not None and tx_sig and lot_source == "tx_exact":
            entry = tx_price
        # Create lot only from parsed transaction (tx_exact). Balance-delta-only creation is disabled.
        if lot_source == "tx_exact" and tx_sig:
            lot_entry_price: Optional[float] = entry if entry > 0 else None
            new_lot = LotInfo.create(
                mint=mint,
                token_amount_raw=unmatched_raw,
                entry_price=lot_entry_price,
                confidence=lot_confidence,  # type: ignore[arg-type]
                source=lot_source,
                entry_confidence=lot_entry_confidence,  # type: ignore[arg-type]
                tx_signature=tx_sig,
                detected_at=tx_detected_at,
            )
            ms.lots = getattr(ms, "lots", None) or []
            ms.lots.append(new_lot)
            if safety_path:
                add_processed_fingerprint(safety_path, fingerprint, max_fp)
            ms.last_known_balance_raw = str(current_raw)
            ms.trading_bag_raw = str(_trading_bag_from_lots(ms))
            if journal_path:
                append_event(journal_path, EVENT_LOT_CREATED, {"mint": mint[:12], "lot_id": new_lot.lot_id[:8], "token_amount_raw": unmatched_raw, "entry_confidence": lot_entry_confidence, "source": lot_source})
                append_event(journal_path, EVENT_LOT_CREATED_TX_EXACT, {"mint": mint[:12], "lot_id": new_lot.lot_id[:8], "tx_signature": tx_sig[:16] if tx_sig else None, "entry_price": entry})
                if project_root and entry and entry > 0:
                    sym = (symbol_by_mint or {}).get(mint, mint[:8])
                    dec = (decimals_by_mint or {}).get(mint, 6)
                    amount_ui = unmatched_raw / (10 ** dec)
                    amt = f"{amount_ui / 1e6:.1f}M" if amount_ui >= 1e6 else (f"{amount_ui / 1e3:.1f}K" if amount_ui >= 1e3 else f"{amount_ui:.2f}")
                    body = f"New buy detected — {sym}  Amount: {amt}  Entry: {entry:.2e} SOL  Source: tx_exact"
                    _notify_founder(project_root, body, "Mint Ladder", critical=False)
            logger.info("BUY_DETECTED mint=%s unmatched_raw=%s source=tx_exact lot_created=1", mint[:12], unmatched_raw)
        else:
            # Balance delta with no tx (e.g. no rpc/wallet): informational only; no tradable lot created.
            logger.warning("BALANCE_DELTA_WITHOUT_TX mint=%s unmatched_raw=%s BALANCE_DELTA_INFORMATIONAL_ONLY (no tradable lot)", mint[:12], unmatched_raw)
            if journal_path:
                append_event(
                    journal_path,
                    EVENT_UNRESOLVED_BALANCE_DELTA,
                    {"mint": mint[:12], "unmatched_raw": unmatched_raw, "reason": "no_rpc_or_wallet", "note": "informational_only_no_lot"},
                )
            if safety_path:
                add_processed_fingerprint(safety_path, unresolved_fp, max_fp)
            ms.last_known_balance_raw = str(current_raw)
            lots = getattr(ms, "lots", None) or []
            if lots:
                ms.trading_bag_raw = str(_trading_bag_from_lots(ms))


def _count_pending_lots(state: RuntimeState) -> int:
    """Count lots with entry_confidence=pending_price_resolution."""
    n = 0
    for ms in state.mints.values():
        for lot in getattr(ms, "lots", None) or []:
            if getattr(lot, "entry_confidence", None) == "pending_price_resolution":
                n += 1
    return n


def _count_display_pending_lots(state: RuntimeState) -> int:
    """Count lots that would display as pending in the dashboard: pending_price_resolution OR (snapshot and source != initial_migration). Must match dashboard_server build_dashboard_payload logic."""
    n = 0
    for ms in state.mints.values():
        for lot in getattr(ms, "lots", None) or []:
            ec = getattr(lot, "entry_confidence", None)
            src = getattr(lot, "source", None)
            if ec == "pending_price_resolution" or (ec == "snapshot" and src != "initial_migration"):
                n += 1
    return n


def _run_cycle_reconciliation_second_pass(state: RuntimeState) -> Tuple[int, int]:
    """Run invalid-exact and display-pending downgrades again so no stale confidence survives. Returns (n_invalid_exact_fixed, n_display_pending_fixed)."""
    n_invalid = _downgrade_invalid_exact_lots(state)
    n_display = _downgrade_display_pending_lots(state)
    return (n_invalid, n_display)


def _run_sniper_cycle(
    state: RuntimeState,
    config: Config,
    rpc: RpcClient,
    pubkey: str,
    sign_fn: Any,
    state_path: Path,
    status_path: Path,
    event_journal_path: Optional[Path],
    safety_path: Optional[Path],
    run_state: Dict[str, Any],
) -> None:
    """
    Optional sniper cycle: detect_all → filter → execute_buy → confirm_fill → create_lot → persist.
    One buy per cycle when gating passes. No lot creation before confirm_fill. Emits BUY_SENT, BUY_CONFIRMED, etc.
    """
    if not getattr(config, "sniper_enabled", False):
        return


def _consume_manual_override(
    mint_state: RuntimeMintState,
    amount_raw: int,
    journal_path: Optional[Path],
    mint_addr: str,
    tx_signature: Optional[str],
) -> int:
    """
    Consume amount_raw from manual_override_inventory (approved records only), FIFO-style.

    Returns actual amount consumed (<= amount_raw). Updates manual_override_sold_raw and emits audit event.
    """
    if amount_raw <= 0:
        return 0
    records = getattr(mint_state, "manual_override_inventory", None) or []
    remaining = amount_raw
    consumed = 0
    for rec in records:
        try:
            if not getattr(rec, "operator_approved", False):
                continue
            amt = int(getattr(rec, "amount_raw", 0) or 0)
        except (ValueError, TypeError):
            continue
        if amt <= 0:
            continue
        take = min(amt, remaining)
        if take <= 0:
            continue
        new_amt = amt - take
        rec.amount_raw = new_amt
        remaining -= take
        consumed += take
        if remaining <= 0:
            break
    if consumed <= 0:
        return 0
    try:
        prev = int(getattr(mint_state, "manual_override_sold_raw", "0") or 0)
    except (ValueError, TypeError):
        prev = 0
    mint_state.manual_override_sold_raw = str(prev + consumed)
    # Emit audit event
    if journal_path:
        try:
            total_override_remaining = 0
            for rec in getattr(mint_state, "manual_override_inventory", None) or []:
                try:
                    total_override_remaining += int(getattr(rec, "amount_raw", 0) or 0)
                except (ValueError, TypeError):
                    continue
            append_event(
                journal_path,
                MANUAL_OVERRIDE_CONSUMED,
                {
                    "mint": mint_addr[:12],
                    "amount_raw": consumed,
                    "remaining_override_raw": total_override_remaining,
                    "tx_sig": (tx_signature or "")[:22] if tx_signature else None,
                },
            )
        except Exception:
            pass
    return consumed
    now = time.monotonic()
    last = run_state.get("last_sniper_cycle_time") or 0
    if now - last < getattr(config, "sniper_cooldown_seconds", 30.0):
        return
    try:
        sol_lamports = rpc.get_balance(pubkey)
    except Exception:
        return
    sol_balance = sol_lamports / 1e9
    if sol_balance < getattr(config, "sniper_min_sol_reserve", 0.1):
        logger.info("SNIPER_SKIP sol_balance=%.4f below SNIPER_MIN_SOL_RESERVE", sol_balance)
        return
    total_lots = sum(len(getattr(ms, "lots", None) or []) for ms in state.mints.values())
    if total_lots >= getattr(config, "sniper_max_concurrent_lots", 5):
        logger.info("SNIPER_SKIP total_lots=%d >= SNIPER_MAX_CONCURRENT_LOTS", total_lots)
        return

    from .sniper_engine import detect_all, filter_candidate
    from .sniper_engine.integration import create_lot_in_state, persist_sniper_lot
    from .sniper_engine.sniper_executor import confirm_fill, execute_buy
    from .sniper_engine.sniper_failures import (
        record_failure,
        REASON_BUILD_SWAP_FAILED,
        REASON_SEND_FAILED,
        REASON_CONFIRM_FILL_FAILED,
        REASON_OTHER,
    )
    from .sniper_engine.deployer_reputation import record_rejected, record_successful_trade
    from .strategy import compute_trading_bag

    buy_sol = getattr(config, "sniper_buy_sol", 0.02)
    skip_existing = getattr(config, "sniper_skip_existing_mints", True)
    decimals_default = 6
    runtime_dir = state_path.parent / "runtime"
    deployer_history_path = runtime_dir / "reputation" / "deployer_history.json"
    sniper_failures_path = runtime_dir / "stats" / "sniper_failures.json"

    candidates = detect_all(limit_per_source=10)
    for c in candidates:
        if skip_existing and c.mint in state.mints:
            continue
        fr = filter_candidate(
            c,
            require_metadata=False,
            min_liquidity_usd=0.0,
            rpc=rpc,
            deployer_history_path=deployer_history_path,
        )
        if not fr.passed:
            if event_journal_path:
                append_event(event_journal_path, TOKEN_FILTER_REJECTED, {"mint": c.mint[:12], "reason": fr.reason})
            deployer = (c.metadata or {}).get("deployer", "").strip()
            if deployer and deployer_history_path:
                record_rejected(deployer_history_path, deployer)
            continue
        if event_journal_path:
            append_event(event_journal_path, TOKEN_FILTER_APPROVED, {"mint": c.mint[:12]})
        if event_journal_path:
            append_event(event_journal_path, BUY_SENT, {"mint": c.mint[:12], "amount_sol": buy_sol})
        result = execute_buy(
            c.mint,
            buy_sol,
            pubkey,
            sign_fn,
            config,
            rpc,
            slippage_bps=config.slippage_bps,
        )
        if not result.success or not result.tx_signature:
            fail_reason = (result.error or "no_sig").lower()
            if event_journal_path:
                append_event(
                    event_journal_path,
                    BUY_FAILED,
                    {"mint": c.mint[:12], "reason": "tx_error", "error": result.error or "no_sig"},
                )
            if sniper_failures_path:
                record_failure(
                    sniper_failures_path,
                    c.mint,
                    REASON_SEND_FAILED if "send" in fail_reason else REASON_BUILD_SWAP_FAILED if "build" in fail_reason else REASON_OTHER,
                    tx_error=result.error,
                )
            continue
        ok, token_raw, entry_price = confirm_fill(
            result.tx_signature,
            c.mint,
            pubkey,
            expected_min_raw=1,
            rpc=rpc,
            decimals=decimals_default,
        )
        if not ok or token_raw <= 0:
            if event_journal_path:
                append_event(
                    event_journal_path,
                    BUY_FAILED,
                    {"mint": c.mint[:12], "reason": "confirm_fill_failed"},
                )
            if sniper_failures_path:
                record_failure(sniper_failures_path, c.mint, REASON_CONFIRM_FILL_FAILED)
            continue
        if check_duplicate_lot_for_tx(state, result.tx_signature, c.mint, event_journal_path):
            continue
        entry_price = entry_price or 0.0
        trading_bag_raw, moonbag_raw = compute_trading_bag(str(token_raw), config.trading_bag_pct)
        lot = create_lot_in_state(
            state,
            c.mint,
            token_raw,
            entry_price,
            result.tx_signature,
            str(trading_bag_raw),
            str(moonbag_raw),
            program_or_venue=c.source if c.source != "test" else "jupiter",
        )
        persist_sniper_lot(
            state_path,
            status_path,
            state,
            journal_path=event_journal_path,
            created_lot=lot,
            token_raw=token_raw,
            buy_sol=buy_sol,
        )
        if event_journal_path:
            append_event(event_journal_path, LADDER_ARMED, {"mint": c.mint[:12], "lot_id": lot.lot_id[:8]})
        deployer = (c.metadata or {}).get("deployer", "").strip()
        if deployer and deployer_history_path:
            record_successful_trade(deployer_history_path, deployer)
        if safety_path:
            add_processed_signature(safety_path, result.tx_signature)
        ms = state.mints.get(c.mint)
        if ms:
            check_lot_invariants(c.mint, ms, event_journal_path)
        run_state["last_sniper_cycle_time"] = time.monotonic()
        lots_active = sum(len(getattr(ms, "lots", None) or []) for ms in state.mints.values())
        now_utc = datetime.now(tz=timezone.utc)
        today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        lots_created_today = sum(
            1
            for ms in state.mints.values()
            for lot in getattr(ms, "lots", None) or []
            if getattr(lot, "detected_at", None) and lot.detected_at >= today_start
        )
        lots_closed = sum(
            1
            for ms in state.mints.values()
            for lot in getattr(ms, "lots", None) or []
            if getattr(lot, "status", None) == "fully_sold"
        )
        logger.info(
            "SNIPER_LOT_CREATED mint=%s lot_id=%s tx_sig=%s token_raw=%s entry=%.6e buy_sol=%.4f",
            c.mint[:12], lot.lot_id[:8], result.tx_signature[:16], token_raw, entry_price, buy_sol,
        )
        logger.info(
            "LADDER_ARMED mint=%s lot_id=%s | lots_active=%d lots_created_today=%d lots_closed=%d",
            c.mint[:12], lot.lot_id[:8], lots_active, lots_created_today, lots_closed,
        )
        return


def _downgrade_invalid_exact_lots(state: RuntimeState) -> int:
    """Downgrade exact lots with out-of-range entry_price to unknown. Returns count fixed."""
    fixed = 0
    for _mint, ms in state.mints.items():
        for lot in getattr(ms, "lots", None) or []:
            if getattr(lot, "entry_confidence", None) != "exact":
                continue
            ep = getattr(lot, "entry_price_sol_per_token", None)
            if ep is not None and not validate_entry_price(ep):
                lot.entry_confidence = "unknown"  # type: ignore[assignment]
                lot.entry_price_sol_per_token = None  # type: ignore[assignment]
                lot.cost_basis_confidence = "unknown"  # type: ignore[assignment]
                fixed += 1
    return fixed


def _downgrade_display_pending_lots(state: RuntimeState) -> int:
    """Downgrade lots that would display as pending (snapshot + source != initial_migration) to unknown so they are not shown as pending_price_resolution forever. Returns count downgraded."""
    downgraded = 0
    for _mint, ms in state.mints.items():
        for lot in getattr(ms, "lots", None) or []:
            ec = getattr(lot, "entry_confidence", None)
            src = getattr(lot, "source", None)
            if ec != "snapshot" or src == "initial_migration":
                continue
            lot.entry_confidence = "unknown"  # type: ignore[assignment]
            lot.entry_price_sol_per_token = None  # type: ignore[assignment]
            lot.cost_basis_confidence = "unknown"  # type: ignore[assignment]
            downgraded += 1
    return downgraded


def _resolve_pending_price_lots(
    state: RuntimeState,
    rpc: Optional[RpcClient],
    wallet_pubkey: Optional[str],
    config: Config,
    decimals_by_mint: Dict[str, int],
    journal_path: Optional[Path] = None,
) -> int:
    """
    Every cycle: reprocess lots with entry_confidence=pending_price_resolution.
    Attempt find_buy_tx_for_delta (up to ENTRY_SCAN_MAX_SIGNATURES). If found, update lot entry.
    If not found after full scan, set entry_confidence=unknown (no permanent pending).
    Returns count of lots resolved.
    """
    if not rpc or not wallet_pubkey:
        return 0
    from .tx_infer import find_buy_tx_for_delta

    pending_before = _count_pending_lots(state)
    resolved = 0
    downgraded = 0
    for mint, ms in state.mints.items():
        lots = getattr(ms, "lots", None) or []
        existing_sigs: Set[str] = set()
        for l in lots:
            s = getattr(l, "tx_signature", None)
            if s:
                existing_sigs.add(s)
        for lot in lots:
            ec = getattr(lot, "entry_confidence", None)
            if ec != "pending_price_resolution":
                continue
            ep = getattr(lot, "entry_price_sol_per_token", None)
            if ep is not None and ep > 0:
                continue
            try:
                token_amount_raw = int(getattr(lot, "token_amount", 0) or 0)
            except (ValueError, TypeError):
                lot.entry_confidence = "unknown"  # type: ignore[assignment]
                downgraded += 1
                if journal_path:
                    append_event(journal_path, "TX_LOOKUP_FAILED", {"mint": mint[:12], "lot_id": getattr(lot, "lot_id", "")[:8], "reason": "invalid_token_amount"})
                continue
            if token_amount_raw <= 0:
                lot.entry_confidence = "unknown"  # type: ignore[assignment]
                downgraded += 1
                continue
            dec = decimals_by_mint.get(mint, 6)
            buy_tx = find_buy_tx_for_delta(
                wallet_pubkey,
                mint,
                token_amount_raw,
                rpc,
                max_signatures=ENTRY_SCAN_MAX_SIGNATURES,
                exclude_signatures=existing_sigs,
                decimals=dec,
            )
            if buy_tx:
                sig, price, when = buy_tx
                if price is not None and validate_entry_price(price):
                    lot.tx_signature = sig
                    lot.entry_price_sol_per_token = price
                    lot.detected_at = when
                    lot.source = "tx_exact"
                    lot.entry_confidence = "exact"  # type: ignore[assignment]
                    lot.cost_basis_confidence = "known"  # type: ignore[assignment]
                    existing_sigs.add(sig)
                    resolved += 1
                    if journal_path:
                        append_event(journal_path, EVENT_LOT_CREATED_TX_EXACT, {"mint": mint[:12], "lot_id": getattr(lot, "lot_id", "")[:8], "tx_signature": sig[:16], "entry_price": price, "reason": "pending_resolved"})
                    logger.info("PENDING_LOT_RESOLVED mint=%s lot_id=%s price=%.6e", mint[:12], lot.lot_id[:8], price)
                else:
                    lot.entry_confidence = "unknown"  # type: ignore[assignment]
                    downgraded += 1
                    if journal_path and price is not None:
                        append_event(journal_path, "PRICE_SANITY_REJECTED", {"mint": mint[:12], "lot_id": getattr(lot, "lot_id", "")[:8], "price": price})
            else:
                lot.entry_confidence = "unknown"  # type: ignore[assignment]
                downgraded += 1
                if journal_path:
                    append_event(journal_path, "TX_LOOKUP_FAILED", {"mint": mint[:12], "lot_id": getattr(lot, "lot_id", "")[:8], "reason": "no_matching_tx_after_scan"})
    pending_after = _count_pending_lots(state)
    logger.info(
        "RESOLVER_STATUS pending_before=%d resolved=%d downgraded=%d pending_after=%d",
        pending_before, resolved, downgraded, pending_after,
    )
    return resolved


def _backfill_lot_tx(
    state: RuntimeState,
    wallet_pubkey: str,
    rpc: RpcClient,
    config: Config,
    journal_path: Optional[Path] = None,
) -> int:
    """
    One-off: try to enrich snapshot lots with tx_signature and real buy price from chain.
    Returns count of lots enriched. Only touches lots with no tx_signature and entry_confidence snapshot.
    """
    from .tx_infer import find_buy_tx_for_delta

    enriched = 0
    max_sigs = min(max(20, getattr(config, "entry_infer_signature_limit", 60) or 60), ENTRY_SCAN_MAX_SIGNATURES)
    for mint, ms in state.mints.items():
        lots = getattr(ms, "lots", None) or []
        existing_sigs = {getattr(l, "tx_signature", None) for l in lots if getattr(l, "tx_signature", None)}
        for lot in lots:
            if getattr(lot, "tx_signature", None):
                continue
            ec = getattr(lot, "entry_confidence", "snapshot")
            if ec not in ("snapshot", "pending_price_resolution"):
                continue
            try:
                token_amount_raw = int(getattr(lot, "token_amount", 0) or 0)
            except (ValueError, TypeError):
                if journal_path:
                    append_event(journal_path, EVENT_BUY_BACKFILL_SKIPPED, {"mint": mint[:12], "lot_id": getattr(lot, "lot_id", "")[:8], "reason": "invalid_token_amount"})
                continue
            if token_amount_raw <= 0:
                continue
            buy_tx = find_buy_tx_for_delta(
                wallet_pubkey, mint, token_amount_raw, rpc, max_signatures=max_sigs, exclude_signatures=existing_sigs, decimals=6
            )
            if not buy_tx:
                if journal_path:
                    append_event(journal_path, EVENT_BUY_BACKFILL_SKIPPED, {"mint": mint[:12], "lot_id": getattr(lot, "lot_id", "")[:8], "reason": "no_matching_tx"})
                continue
            sig, price, when = buy_tx
            if sig in existing_sigs:
                logger.info("LOT_TX_REUSED_BLOCKED mint=%s lot_id=%s sig=%s (tx already used for another lot)", mint[:12], getattr(lot, "lot_id", "")[:8], sig[:16])
                if journal_path:
                    append_event(journal_path, EVENT_BUY_BACKFILL_SKIPPED, {"mint": mint[:12], "lot_id": getattr(lot, "lot_id", "")[:8], "reason": "tx_already_used"})
                continue
            if price is not None and not validate_entry_price(price):
                logger.warning("LOT_PRICE_SANITY_FAILED backfill mint=%s lot_id=%s price=%.6e; not upgrading to tx_exact", mint[:12], getattr(lot, "lot_id", "")[:8], price)
                if journal_path:
                    append_event(journal_path, EVENT_BUY_BACKFILL_SKIPPED, {"mint": mint[:12], "lot_id": getattr(lot, "lot_id", "")[:8], "reason": "price_outside_bounds"})
                continue
            lot.tx_signature = sig
            lot.entry_price_sol_per_token = price
            lot.detected_at = when
            lot.source = "tx_exact"
            lot.entry_confidence = "exact"  # type: ignore[assignment]
            lot.cost_basis_confidence = "known"  # type: ignore[assignment]
            existing_sigs.add(sig)
            enriched += 1
            if journal_path:
                append_event(journal_path, EVENT_BUY_BACKFILL_CREATED, {"mint": mint[:12], "lot_id": getattr(lot, "lot_id", "")[:8], "tx_signature": sig[:16]})
            logger.info("BACKFILL_LOT_TX mint=%s lot_id=%s tx=%s price=%.6e", mint[:12], getattr(lot, "lot_id", "")[:8], sig[:16], price)
    return enriched


def _is_paused(mint_state) -> bool:
    paused_until = mint_state.failures.paused_until
    if paused_until is None:
        return False
    return datetime.now(tz=timezone.utc) < paused_until




def _update_reconciliation_pause_for_mint(
    mint: str,
    mint_state: RuntimeMintState,
    actual_raw: int,
    sum_lots: int,
    now: datetime,
    config: Config,
    event_journal_path: Optional[Path],
) -> None:
    """
    Per-mint reconciliation guard: if wallet_balance != sum_active_lots for
    RECONCILE_MISMATCH_CONSECUTIVE_CYCLES in a row, pause this mint for a
    short window so ladder does not trade on uncertain inventory.
    """
    mismatch = sum_lots != actual_raw
    # Initialize fields for robustness against legacy state.
    consecutive = getattr(mint_state, "reconcile_mismatch_consecutive", 0) or 0
    last_seen = getattr(mint_state, "reconcile_mismatch_last_seen_at", None)

    if mismatch:
        consecutive += 1
        mint_state.reconcile_mismatch_consecutive = consecutive
        mint_state.reconcile_mismatch_last_seen_at = now
        if consecutive >= RECONCILE_MISMATCH_CONSECUTIVE_CYCLES:
            # Pause only when not already paused for reconciliation.
            pause_min = getattr(config, "reconcile_mismatch_pause_minutes", RECONCILE_MISMATCH_PAUSE_MINUTES) or RECONCILE_MISMATCH_PAUSE_MINUTES
            pause_until = now + timedelta(minutes=pause_min)
            failures = mint_state.failures
            if failures.paused_until is None or failures.paused_until < pause_until:
                failures.paused_until = pause_until
                failures.last_error = "reconciliation_mismatch"
                logger.warning(
                    "%s mint=%s reason=reconciliation_mismatch consecutive=%d paused_minutes=%d until=%s",
                    MINT_PAUSED,
                    mint[:12],
                    consecutive,
                    pause_min,
                    pause_until,
                )
                if event_journal_path:
                    try:
                        append_event(
                            event_journal_path,
                            EVENT_MINT_RECONCILIATION_PAUSED,
                            {
                                "mint": mint[:12],
                                "wallet_balance": actual_raw,
                                "sum_active_lots": sum_lots,
                                "consecutive_mismatches": consecutive,
                                "paused_until": pause_until.isoformat(),
                            },
                        )
                    except Exception:
                        pass
    else:
        # Mismatch cleared this cycle.
        if consecutive > 0:
            prev = consecutive
            mint_state.reconcile_mismatch_consecutive = 0
            mint_state.reconcile_mismatch_last_seen_at = None
            logger.info(
                "RECONCILIATION_MISMATCH_RECOVERED mint=%s wallet_balance=%s sum_active_lots=%s previous_consecutive=%d",
                mint[:12],
                actual_raw,
                sum_lots,
                prev,
            )
            if event_journal_path:
                try:
                    append_event(
                        event_journal_path,
                        EVENT_MINT_RECONCILIATION_RECOVERED,
                        {
                            "mint": mint[:12],
                            "wallet_balance": actual_raw,
                            "sum_active_lots": sum_lots,
                            "previous_consecutive_mismatches": prev,
                        },
                    )
                except Exception:
                    pass
        # If we were paused specifically for reconciliation and the window has elapsed,
        # clear the error tag so future pauses are attributable.
        failures = mint_state.failures
        if failures.last_error == "reconciliation_mismatch" and (
            failures.paused_until is None or now >= failures.paused_until
        ):
            failures.last_error = None


def _check_liquidity_collapse(
    mint_status: MintStatus,
    mint_state: RuntimeMintState,
    config: Config,
) -> None:
    """
    Rug / liquidity-collapse guard: compare current liquidity to a reference level.
    If liquidity disappears or drops sharply, pause the mint (set failures.paused_until and last_error).
    Uses existing FailureInfo so _is_paused() and dashboard show pause as usual.
    """
    now = datetime.now(tz=timezone.utc)
    ds = mint_status.market.dexscreener
    current_usd = ds.liquidity_usd if ds is not None else None
    if current_usd is not None and current_usd <= 0:
        current_usd = None
    reference = getattr(mint_state, "reference_liquidity_usd", None)
    mint_state.last_liquidity_check_at = now

    if current_usd is None:
        if reference is not None and reference > 0:
            pause_until = now + timedelta(minutes=config.liquidity_collapse_pause_minutes)
            mint_state.failures.paused_until = pause_until
            mint_state.failures.last_error = "liquidity_collapse"
            logger.warning(
                "LIQUIDITY_COLLAPSE mint=%s pair=%s: liquidity null/unavailable (reference was %.0f USD); pausing until %s",
                mint_status.mint,
                _pair_name(mint_status),
                reference,
                pause_until,
            )
        return
    if reference is None:
        mint_state.reference_liquidity_usd = current_usd
        return
    if reference < config.liquidity_collapse_min_reference_usd:
        mint_state.reference_liquidity_usd = max(reference, current_usd)
        return
    drop_pct = config.liquidity_collapse_drop_pct
    if current_usd < reference * (1.0 - drop_pct):
        pause_until = now + timedelta(minutes=config.liquidity_collapse_pause_minutes)
        mint_state.failures.paused_until = pause_until
        mint_state.failures.last_error = "liquidity_collapse"
        logger.warning(
            "LIQUIDITY_COLLAPSE mint=%s pair=%s: liquidity dropped from %.0f to %.0f USD (%.0f%% drop); pausing until %s",
            mint_status.mint,
            _pair_name(mint_status),
            reference,
            current_usd,
            drop_pct * 100,
            pause_until,
        )
        return
    mint_state.reference_liquidity_usd = max(reference, current_usd)
    if getattr(mint_state.failures, "last_error", None) == "liquidity_collapse":
        mint_state.failures.last_error = None


def _try_buyback(
    mint_status: MintStatus,
    mint_state,
    rpc: RpcClient,
    config: Config,
    pubkey: str,
    sign_tx: Any,  # callable (tx_base64: str) -> bytes
    status_created_at: datetime,
    trading_disabled: bool = False,
) -> BuybackResult:
    """If buy-back is enabled and trigger/caps pass, execute one SOL→token buy and update state. Respects STOP/trading_disabled (no SOL spent when disabled)."""
    if not getattr(config, "buyback_enabled", False):
        return "skipped"
    if trading_disabled:
        logger.debug("BUYBACK_SKIPPED mint=%s reason=trading_disabled (STOP or RPC pause)", mint_status.mint[:12])
        return "skipped"
    buybacks = getattr(mint_state, "buybacks", None)
    if buybacks is None:
        return "skipped"
    current_price = _get_current_price_sol(
        mint_status=mint_status,
        runtime_state=mint_state,
        rpc=rpc,
        config=config,
        status_created_at=status_created_at,
    )
    if current_price is None:
        return "skipped"
    entry = mint_state.entry_price_sol_per_token
    trigger_price = entry * (1.0 - config.buyback_trigger_pct)
    if current_price > trigger_price:
        return "skipped"
    total_spent = buybacks.total_sol_spent
    if total_spent >= config.buyback_max_sol_per_mint:
        return "skipped"
    now = datetime.now(tz=timezone.utc)
    if buybacks.last_buy_at is not None:
        elapsed = (now - buybacks.last_buy_at).total_seconds()
        if elapsed < config.buyback_cooldown_sec:
            return "skipped"
    lamports = rpc.get_balance(pubkey)
    wallet_sol = lamports / 1e9
    available = wallet_sol - config.buyback_sol_reserve
    if available < config.buyback_min_sol:
        return "skipped"
    spend_sol = min(
        config.buyback_max_sol_per_trade,
        config.buyback_max_sol_per_mint - total_spent,
        available,
    )
    if spend_sol < config.buyback_min_sol:
        return "skipped"
    if not _enforce_liquidity_guard(mint_status, config):
        return "skipped"
    amount_lamports = int(spend_sol * 1e9)
    if amount_lamports <= 0:
        return "skipped"
    # Risk engine: block buyback when limits violated (CEO directive).
    liquidity_usd = None
    if mint_status.market and mint_status.market.dexscreener:
        liquidity_usd = getattr(mint_status.market.dexscreener, "liquidity_usd", None)
    risk_reason = block_execution_reason(
        liquidity_usd=liquidity_usd,
        slippage_bps=config.slippage_bps,
        trade_sol=spend_sol,
        wallet_sol=wallet_sol,
        sold_this_hour_sol=0.0,
        trading_bag_sol_value=1.0,
        limits=RiskLimits(),
    )
    if risk_reason is not None:
        logger.warning("%s buyback mint=%s symbol=%s reason=%s", RISK_BLOCK, mint_status.mint[:12], _pair_name(mint_status), risk_reason)
        return "skipped"
    try:
        exec_result = execute_swap(
            input_mint=WSOL_MINT,
            output_mint=mint_status.mint,
            amount_raw=amount_lamports,
            user_pubkey=pubkey,
            config=config,
            rpc=rpc,
            sign_fn=sign_tx,
        )
        if not exec_result.success:
            raise RuntimeError(exec_result.error or "execute_swap failed")
        signature = exec_result.signature or ""
        if not exec_result.confirmed:
            raise RuntimeError(f"Transaction {signature} not confirmed in time")
        buybacks.total_sol_spent += spend_sol
        buybacks.last_buy_at = now
        buybacks.last_sig = signature
        logger.info(
            "Buy-back %s: spent %.6f SOL, sig=%s",
            _pair_name(mint_status),
            spend_sol,
            signature,
        )
        return "executed"
    except (JupiterError, WalletError, Exception) as exc:
        logger.warning(
            "Buy-back failed for %s: %s",
            _pair_name(mint_status),
            exc,
        )
        return "failed"


def _within_hour_cap(
    mint: str,
    trading_bag_raw: int,
    candidate_sell_raw: int,
    per_mint_sells: Dict[str, List[Tuple[datetime, int]]],
    config: Config,
) -> bool:
    now = datetime.now(tz=timezone.utc)
    events = per_mint_sells.get(mint, [])
    # Drop sells older than 1 hour.
    events = [e for e in events if (now - e[0]) <= timedelta(hours=1)]
    per_mint_sells[mint] = events
    already = sum(amount for _, amount in events)
    cap = int(trading_bag_raw * config.max_sell_bag_fraction_per_hour)
    return already + candidate_sell_raw <= cap


def _within_24h_cap(
    mint: str,
    trading_bag_raw: int,
    candidate_sell_raw: int,
    per_mint_sells_24h: Dict[str, List[Tuple[datetime, int]]],
    config: Config,
) -> bool:
    """Rolling 24h sell cap per mint to protect against runaway behavior."""
    now = datetime.now(tz=timezone.utc)
    events = per_mint_sells_24h.get(mint, [])
    events = [e for e in events if (now - e[0]) <= timedelta(hours=24)]
    per_mint_sells_24h[mint] = events
    already = sum(amount for _, amount in events)
    cap = int(trading_bag_raw * config.max_sell_bag_fraction_per_24h)
    return already + candidate_sell_raw <= cap


def _audit_sell(
    mint: str,
    symbol: Optional[str],
    step_id: int,
    target_multiple: float,
    entry_price: float,
    quoted_price: Optional[float],
    sell_amount_raw: int,
    sell_ui: float,
    liquidity_cap_raw: Optional[int],
    cooldown_state: str,
    action: Literal["executed", "skipped"],
    reason: str,
) -> None:
    """Structured audit log for every sell attempt (executed or skipped)."""
    if action == "skipped":
        logger.info(
            "SELL_SKIPPED_REASON mint=%s symbol=%s step_id=%s reason=%s",
            mint, symbol or mint[:8], step_id, reason,
        )
    logger.info(
        "AUDIT sell mint=%s symbol=%s step_id=%s target_mult=%.4f entry=%.6e quoted=%.6e sell_raw=%s sell_ui=%.6f liq_cap=%s cooldown=%s action=%s reason=%s",
        mint,
        symbol or "",
        step_id,
        target_multiple,
        entry_price,
        quoted_price if quoted_price is not None else 0.0,
        sell_amount_raw,
        sell_ui,
        liquidity_cap_raw if liquidity_cap_raw is not None else "n/a",
        cooldown_state,
        action,
        reason,
    )


def _audit_bootstrap(
    mint: str,
    symbol: Optional[str],
    action: Literal["attempted", "executed", "skipped", "failed"],
    reason: str,
    sol_spent: Optional[float] = None,
    tokens_received: Optional[str] = None,
    derived_entry: Optional[float] = None,
    sig: Optional[str] = None,
) -> None:
    """Structured audit log for bootstrap buy (unknown-entry mint)."""
    logger.info(
        "AUDIT bootstrap mint=%s symbol=%s action=%s reason=%s sol_spent=%s tokens_received=%s derived_entry=%s sig=%s",
        mint,
        symbol or "",
        action,
        reason,
        str(sol_spent) if sol_spent is not None else "n/a",
        tokens_received if tokens_received is not None else "n/a",
        f"{derived_entry:.6e}" if derived_entry is not None else "n/a",
        sig or "n/a",
    )


def _run_bootstrap_buy(
    mint_status: MintStatus,
    mint_state: RuntimeMintState,
    state: RuntimeState,
    state_path: Path,
    rpc: RpcClient,
    config: Config,
    pubkey: str,
    sign_tx: Any,  # callable (tx_base64: str) -> bytes
    trading_disabled: bool,
    monitor_only: bool,
) -> Literal["executed", "skipped", "failed"]:
    """
    Execute one tiny SOL→token buy for unknown-entry mint to establish entry from chain.
    Returns executed/skipped/failed. Only one bootstrap per mint; idempotent if already completed.
    """
    bootstrap = getattr(mint_state, "bootstrap", None) or BootstrapInfo()
    if bootstrap.bootstrap_completed_at is not None:
        return "skipped"
    if trading_disabled:
        _audit_bootstrap(
            mint_status.mint,
            mint_status.symbol,
            "skipped",
            "stop_or_rpc_pause",
        )
        return "skipped"
    if monitor_only:
        _audit_bootstrap(
            mint_status.mint,
            mint_status.symbol,
            "skipped",
            "monitor_only",
        )
        return "skipped"
    if not _enforce_liquidity_guard(mint_status, config):
        _audit_bootstrap(
            mint_status.mint,
            mint_status.symbol,
            "skipped",
            "low_liquidity",
        )
        return "skipped"
    sol_amount = getattr(config, "bootstrap_buy_sol", 0.01)
    if sol_amount < config.min_trade_sol:
        sol_amount = config.min_trade_sol
    amount_lamports = int(sol_amount * 1e9)
    if amount_lamports <= 0:
        _audit_bootstrap(
            mint_status.mint,
            mint_status.symbol,
            "skipped",
            "zero_amount",
        )
        return "skipped"
    try:
        quote = get_quote(
            input_mint=WSOL_MINT,
            output_mint=mint_status.mint,
            amount_raw=amount_lamports,
            slippage_bps=config.slippage_bps,
            config=config,
        )
    except (JupiterError, Exception) as exc:
        logger.warning("Bootstrap quote failed for %s: %s", _pair_name(mint_status), exc)
        _audit_bootstrap(
            mint_status.mint,
            mint_status.symbol,
            "failed",
            "quote_failed",
        )
        return "failed"
    try:
        impact_pct = float(quote.get("priceImpactPct") or 0)
    except (TypeError, ValueError):
        impact_pct = 0.0
    if impact_pct > 5.0:
        _audit_bootstrap(
            mint_status.mint,
            mint_status.symbol,
            "skipped",
            "price_impact",
        )
        return "skipped"
    _audit_bootstrap(
        mint_status.mint,
        mint_status.symbol,
        "attempted",
        "executing",
    )
    try:
        exec_result = execute_swap(
            input_mint=WSOL_MINT,
            output_mint=mint_status.mint,
            amount_raw=amount_lamports,
            user_pubkey=pubkey,
            config=config,
            rpc=rpc,
            sign_fn=sign_tx,
        )
        if not exec_result.success:
            raise RuntimeError(exec_result.error or "execute_swap failed")
        signature = exec_result.signature or ""
        if not exec_result.confirmed:
            raise RuntimeError(f"Transaction {signature} not confirmed in time")
    except (JupiterError, WalletError, Exception) as exc:
        logger.warning("Bootstrap execution failed for %s: %s", _pair_name(mint_status), exc)
        _audit_bootstrap(
            mint_status.mint,
            mint_status.symbol,
            "failed",
            "exception",
        )
        return "failed"
    try:
        tx = rpc.get_transaction(signature)
    except Exception as exc:
        logger.warning("Bootstrap: could not fetch tx %s: %s", signature, exc)
        mint_state.failures.paused_until = datetime.now(tz=timezone.utc) + timedelta(minutes=CONFIRM_UNCERTAIN_PAUSE_MINUTES)
        _audit_bootstrap(
            mint_status.mint,
            mint_status.symbol,
            "failed",
            "tx_fetch_failed",
            sig=signature,
        )
        return "failed"
    if not tx:
        logger.warning("Bootstrap: tx %s returned null result from RPC", signature)
        mint_state.failures.paused_until = datetime.now(tz=timezone.utc) + timedelta(minutes=CONFIRM_UNCERTAIN_PAUSE_MINUTES)
        _audit_bootstrap(
            mint_status.mint,
            mint_status.symbol,
            "failed",
            "tx_null_result",
            sig=signature,
        )
        return "failed"
    sol_delta = _parse_sol_delta_lamports(tx, pubkey)
    token_deltas = _parse_token_deltas_for_mints(tx, pubkey, [mint_status.mint])
    tokens_raw = token_deltas.get(mint_status.mint, 0)
    if sol_delta is None or sol_delta >= 0 or tokens_raw <= 0:
        logger.warning(
            "Bootstrap tx %s: ambiguous result sol_delta=%s tokens_raw=%s",
            signature,
            sol_delta,
            tokens_raw,
        )
        mint_state.failures.paused_until = datetime.now(tz=timezone.utc) + timedelta(minutes=CONFIRM_UNCERTAIN_PAUSE_MINUTES)
        _audit_bootstrap(
            mint_status.mint,
            mint_status.symbol,
            "failed",
            "ambiguous_result",
            sig=signature,
        )
        return "failed"
    meta = tx.get("meta") or {}
    fee = int(meta.get("fee") or 0)
    sol_spent_lamports = abs(sol_delta) - fee
    if sol_spent_lamports <= 0:
        mint_state.failures.paused_until = datetime.now(tz=timezone.utc) + timedelta(minutes=CONFIRM_UNCERTAIN_PAUSE_MINUTES)
        _audit_bootstrap(
            mint_status.mint,
            mint_status.symbol,
            "failed",
            "invalid_sol_delta",
            sig=signature,
        )
        return "failed"
    sol_spent = sol_spent_lamports / 1e9
    tokens_ui = tokens_raw / (10 ** mint_status.decimals)
    derived_entry = sol_spent / tokens_ui if tokens_ui > 0 else 0.0
    if not validate_entry_price(derived_entry):
        logger.warning(
            "Bootstrap %s derived entry %.6e invalid; pausing mint.",
            _pair_name(mint_status),
            derived_entry,
        )
        mint_state.failures.paused_until = datetime.now(tz=timezone.utc) + timedelta(minutes=CONFIRM_UNCERTAIN_PAUSE_MINUTES)
        _audit_bootstrap(
            mint_status.mint,
            mint_status.symbol,
            "failed",
            "invalid_derived_entry",
            sol_spent=sol_spent,
            tokens_received=str(tokens_raw),
            derived_entry=derived_entry,
            sig=signature,
        )
        return "failed"
    now = datetime.now(tz=timezone.utc)
    mint_state.entry_price_sol_per_token = derived_entry
    mint_state.original_entry_price_sol_per_token = derived_entry
    mint_state.working_entry_price_sol_per_token = derived_entry
    mint_state.entry_source = "bootstrap_buy"
    mint_state.bootstrap.bootstrap_pending = False
    mint_state.bootstrap.bootstrap_started_at = mint_state.bootstrap.bootstrap_started_at or now
    mint_state.bootstrap.bootstrap_completed_at = now
    mint_state.bootstrap.bootstrap_sig = signature
    mint_state.bootstrap.bootstrap_sol_spent = sol_spent
    mint_state.bootstrap.bootstrap_tokens_received = str(tokens_raw)
    _audit_bootstrap(
        mint_status.mint,
        mint_status.symbol,
        "executed",
        "ok",
        sol_spent=sol_spent,
        tokens_received=str(tokens_raw),
        derived_entry=derived_entry,
        sig=signature,
    )
    logger.info(
        "Bootstrap completed for %s: entry=%.6e sol_spent=%.6f tokens_raw=%s sig=%s",
        _pair_name(mint_status),
        derived_entry,
        sol_spent,
        tokens_raw,
        signature,
    )
    save_state_atomic(state_path, state)
    return "executed"


def _stop_file_present(state_path: Path) -> bool:
    """Operator kill-switch per Risk: when present at either path, no swaps are sent (trading_disabled). Paths logged at startup via _stop_file_paths(state_path)."""
    cwd = Path.cwd()
    if (cwd / STOP_FILE).exists():
        return True
    if (state_path.parent / STOP_FILE).exists():
        return True
    return False


def _stop_file_paths(state_path: Path) -> str:
    """Return paths checked for STOP file (for banner)."""
    cwd = Path.cwd()
    paths = [str(cwd / STOP_FILE), str(state_path.parent / STOP_FILE)]
    return ", ".join(paths)


def _pre_swap_invariants_ok(
    *,
    mint_state: RuntimeMintState,
    step_key: str,
    step: LadderStep,
    trading_bag_raw: int,
    liq_cap_raw: Optional[int],
    trading_disabled: bool,
    quote_ts: float,
    amount_raw_override: Optional[int] = None,
    quote_max_age_sec: float = QUOTE_MAX_AGE_SEC,
) -> Tuple[bool, str]:
    """
    Centralized invariant check before sending any swap.
    Returns (True, None) if all invariants pass, else (False, reason).
    amount_raw_override: when set (e.g. for a fractured child), check this amount instead of step.sell_amount_raw.

    Coverage: every swap path must call this before get_swap_tx/sign/send. Main ladder sell path (including each
    fractured child) calls it before sending. Bootstrap buy and buy-back do not use ladder steps; they have their own
    guards (liquidity guard, STOP/trading_disabled/monitor_only respected at loop level) and do not bypass STOP,
    trading_disabled, or monitor_only.
    """
    if _is_paused(mint_state):
        return False, "mint_paused"
    if trading_disabled:
        return False, "trading_disabled"
    if step_key in mint_state.executed_steps:
        return False, "step_already_executed"
    if (time.monotonic() - quote_ts) > quote_max_age_sec:
        return False, "quote_stale"
    amt = amount_raw_override if amount_raw_override is not None else step.sell_amount_raw
    if amt <= 0 or amt > trading_bag_raw:
        return False, "sell_exceeds_bag"
    if liq_cap_raw is not None and amt > liq_cap_raw:
        return False, "sell_exceeds_liq_cap"
    return True, ""


def _run_startup_dry_validation(
    tradable_mints: List[MintStatus],
    state: RuntimeState,
    config: Config,
    rpc: RpcClient,
) -> None:
    """Validate wallet, Jupiter path, and liquidity; print summary; pause mints that fail validation."""
    logger.info("Startup dry validation: validating wallet and Jupiter path per mint.")
    for m in tradable_mints:
        ms = state.mints.get(m.mint)
        if ms is None:
            continue
        probe_raw = max(int(ms.trading_bag_raw) // 10000, 1)
        quote = get_quote_quick(
            input_mint=m.mint,
            output_mint=WSOL_MINT,
            amount_raw=probe_raw,
            slippage_bps=config.slippage_bps,
            config=config,
            timeout_s=8.0,
        )
        if not quote or not quote.get("outAmount"):
            logger.warning(
                "Startup validation: mint %s Jupiter quote failed; pausing mint for %s minutes.",
                _pair_name(m),
                STARTUP_VALIDATION_PAUSE_MINUTES,
            )
            ms.failures.paused_until = datetime.now(tz=timezone.utc) + timedelta(
                minutes=STARTUP_VALIDATION_PAUSE_MINUTES
            )

    for m in tradable_mints:
        ms = state.mints.get(m.mint)
        if ms is None:
            continue
        entry = _working_entry(ms)
        liq_cap = ms.liquidity_cap.max_sell_raw
        cooldown = str(ms.cooldown_until) if ms.cooldown_until else "none"
        ctx = DynamicContext(
            volatility_regime=ms.volatility.regime,
            momentum_regime=ms.momentum.regime,
            liquidity_cap_raw=liq_cap,
        )
        steps = build_dynamic_ladder_for_mint(m, ms, ctx)
        first_step = steps[0] if steps else None
        first_step_str = (
            f"step_id={first_step.step_id} mult={first_step.multiple:.2f} sell_raw={first_step.sell_amount_raw}"
            if first_step
            else "n/a"
        )
        logger.info(
            "Startup summary: mint=%s entry=%.6e first_step=%s liq_cap=%s cooldown=%s",
            _pair_name(m),
            entry,
            first_step_str,
            liq_cap if liq_cap is not None else "n/a",
            cooldown,
        )


def run_bot(
    status_path: Path,
    state_path: Path,
    config: Config,
    monitor_only: bool = False,
    single_cycle: bool = False,
    wallet_id: Optional[str] = None,
    max_cycles: Optional[int] = None,
) -> None:
    """
    Run the live trading loop until killed (or max_cycles reached).
    When monitor_only is True, never send swaps; full loop and audit logs still run.
    When single_cycle is True, run one cycle and return (used by run_one_wallet_lane).
    When wallet_id is set with monitor_only, keypair is not loaded (multi-wallet dry-run).
    When max_cycles is set, exit after that many cycles (e.g. for runtime validation).
    """
    # Validate RPC first so we fail fast before loading wallet/status (no endpoint URL in logs).
    _rpc = RpcClient(config.rpc_endpoint, timeout_s=config.rpc_timeout_s, max_retries=config.max_retries)
    rpc_ok, rpc_latency_ms = _rpc.validate()
    _rpc.close()
    if not rpc_ok:
        logger.error(
            "RPC validation failed: endpoint unreachable (check RPC_ENDPOINT in .env). Exiting."
        )
        raise RuntimeError("RPC validation failed: endpoint unreachable")
    logger.info("RPC validation OK (latency_ms=%.0f)", rpc_latency_ms)
    try:
        rpc_host = urlparse(config.rpc_endpoint).netloc or "unknown"
    except Exception:
        rpc_host = "unknown"
    logger.info("RPC endpoint host: %s", rpc_host)

    from .models import StatusFile  # local import to avoid cycles

    # Wallet / keypair: when wallet_id set use wallet_manager (signing boundary); when None use resolve_keypair(None) for backward compat.
    if monitor_only and wallet_id is not None:
        keypair = None
        pubkey = wallet_id
        logger.info("Monitor-only mode for wallet_id=%s (no keypair loaded)", wallet_id)
    elif wallet_id is not None and wallet_id != "":
        keypair = None
        pubkey = str(wallet_manager.resolve_identity(wallet_id))
        logger.info("Loaded wallet_id=%s pubkey=%s (signing via wallet_manager)", wallet_id, pubkey)
    else:
        keypair = wallet_manager.resolve_keypair(None)
        pubkey = str(keypair.pubkey())
        logger.info("Loaded wallet with public key %s (single-wallet)", pubkey)

    def sign_tx(tx_base64: str) -> bytes:
        if wallet_id is not None and wallet_id != "":
            return wallet_manager.sign_transaction(wallet_id, tx_base64)
        return sign_swap_tx(tx_base64, keypair)

    # Load status and initial state.
    status_data = StatusFile.model_validate_json(status_path.read_text())
    state: RuntimeState = load_state(state_path, status_path)
    sniper_service = SniperService(config=config, state=state)
    # Sync entry from state -> status so status reflects tx-derived entry (single write path).
    from .status_snapshot import write_status_synced
    try:
        write_status_synced(status_data, state, status_path)
    except Exception as e:
        logger.debug("Failed to persist status after entry sync: %s", e)
    # CEO directive: validate state schema on startup; do not run with invalid state.
    schema_errors = validate_state_schema(state)
    if schema_errors:
        logger.error(
            "STATE_SCHEMA_INVALID on startup: %s — stopping to prevent corrupt state.",
            schema_errors[:10],
        )
        raise RuntimeError(f"Invalid state schema: {schema_errors[:5]}")

    # Backfill reanchor fields for legacy state.
    for ms in state.mints.values():
        if getattr(ms, "original_entry_price_sol_per_token", None) is None:
            ms.original_entry_price_sol_per_token = ms.entry_price_sol_per_token
        if getattr(ms, "working_entry_price_sol_per_token", None) is None:
            ms.working_entry_price_sol_per_token = ms.entry_price_sol_per_token
    # CEO: backfill sold_bot_raw / sold_external_raw from executed_steps if missing (pre-migration state).
    for ms in state.mints.values():
        _ensure_sell_accounting_backfill(ms)
    if getattr(state, "session_start_sol", None) is None and state.sol is not None:
        state.session_start_sol = state.sol.sol

    safety_path = state_path.parent / "safety_state.json"
    event_journal_path = state_path.parent / "events.jsonl"

    # Telegram command center: set context and start command receiver (optional; never fail)
    try:
        from .integration.telegram_bot import set_bot_context, start_bot, send_startup_message
        project_root = state_path.parent.parent if state_path.parent.name in ("runtime", "data") else state_path.parent
        set_bot_context(data_dir=state_path.parent, state_path=state_path, status_path=status_path, project_root=project_root, event_journal_path=event_journal_path)
        start_bot()
        send_startup_message(
            rpc_ok=True,
            detection_active=True,
            wallet_loaded=True,
            runtime_clean=os.getenv("CLEAN_START", "").strip().lower() in ("1", "true", "yes"),
        )
        if os.getenv("TTS_ENABLED", "").strip().lower() in ("true", "1", "yes") and os.getenv("TTS_SPEAK_STARTUP", "true").strip().lower() in ("true", "1", "yes"):
            try:
                from mint_ladder_bot.voice import speak
                speak("Avizinki system online.", category="startup")
            except Exception:
                pass
        # Optional: one startup validation message through report() → Telegram + event_bus → voice
        if os.getenv("TG_STARTUP_TEST_MESSAGE_ENABLED", "false").strip().lower() in ("true", "1", "yes"):
            try:
                from mint_ladder_bot.integration.telegram_events import report
                sender = os.getenv("TG_STARTUP_TEST_MESSAGE_SENDER", "Execution Engine").strip() or "Execution Engine"
                text = os.getenv("TG_STARTUP_TEST_MESSAGE_TEXT", "hello miss jackson").strip() or "hello miss jackson"
                report(sender, text)
                logger.debug("Startup test message sent (Telegram + voice): %s: %s", sender, text[:40])
            except Exception as e:
                logger.warning("Startup test message failed: %s", e)
        # Optional: voice + Telegram greeting (report() → send_message → event_bus → voice)
        if os.getenv("TG_GREETING_TEST_ENABLED", "false").strip().lower() in ("true", "1", "yes"):
            try:
                from mint_ladder_bot.integration.telegram_events import report
                report("Execution Engine", "hi avi nice to meet you")
                logger.debug("Greeting test sent (Telegram + voice): hi avi nice to meet you")
            except Exception as e:
                logger.warning("Greeting test failed: %s", e)
        # Optional: team voice roll call — each role sends text greeting (report) and optionally voice (TTS → file → send_voice_message). Flag-controlled.
        _rollcall_enabled = os.getenv("TG_TEAM_GREETING_ROLLCALL", "false").strip().lower() in ("true", "1", "yes")
        _rollcall_once = os.getenv("TG_TEAM_GREETING_ROLLCALL_ONCE", "true").strip().lower() in ("true", "1", "yes")
        _owner_name = (os.getenv("TG_TEAM_GREETING_OWNER_NAME", "Avi").strip() or "Avi")
        _send_text = os.getenv("TG_SEND_TEXT_MESSAGES", "true").strip().lower() in ("true", "1", "yes")
        _send_voice = os.getenv("TG_SEND_VOICE_MESSAGES", "true").strip().lower() in ("true", "1", "yes")
        if _rollcall_enabled:
            data_dir = state_path.parent
            sentinel = data_dir / "runtime" / "telegram" / ".rollcall_sent"
            if _rollcall_once and sentinel.exists():
                logger.debug("Team greeting roll call skipped (already sent once; sentinel exists)")
            else:
                try:
                    from mint_ladder_bot.integration.telegram_events import report
                    from mint_ladder_bot.integration.telegram_bot import send_voice_message
                    # Voice readiness: TTS provider + ffmpeg (for wav→ogg). Skip voice gracefully if not ready.
                    _voice_ready = True
                    if _send_voice:
                        try:
                            import subprocess
                            subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
                        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
                            _voice_ready = False
                            logger.warning("VOICE_READINESS: ffmpeg not available; skipping Telegram voice.")
                        if _voice_ready:
                            try:
                                from workspace_services.voice_service import synthesize_to_file
                                _ = synthesize_to_file
                            except Exception:
                                _voice_ready = False
                                logger.warning("VOICE_READINESS: voice_service not available; skipping Telegram voice.")
                    rollcall_pairs = [
                        ("CTO", f"{_owner_name}, tech stack is solid and ready for you."),
                        ("DevOps", f"{_owner_name}, pipelines are up. DevOps standing by."),
                        ("Execution Engine", f"{_owner_name}, execution engine armed and ready."),
                        ("Risk Engine", f"{_owner_name}, risk guards active. We are within limits."),
                        ("QA", f"{_owner_name}, QA checks passed. Systems green."),
                        ("Launch Detector", f"{_owner_name}, launch detector online. Watching for new mints."),
                        ("Sniper Engine", f"{_owner_name}, sniper engine ready when you are."),
                        ("Ladder Engine", f"{_owner_name}, ladder engine online. Ladders configured and ready."),
                    ]
                    for role, text in rollcall_pairs:
                        if _send_text:
                            report(role, text)
                        if _send_voice and _voice_ready:
                            from workspace_services.voice_service import synthesize_to_file
                            outgoing_dir = data_dir / "runtime" / "voice" / "outgoing"
                            voice_path = synthesize_to_file(text, role, outgoing_dir)
                            if voice_path:
                                send_voice_message(voice_path, caption=text[:200], label=f"rollcall_{role}", from_rollcall=True, sender_role=role)
                    if _rollcall_once:
                        sentinel.parent.mkdir(parents=True, exist_ok=True)
                        sentinel.write_text("", encoding="utf-8")
                    logger.debug("Team greeting roll call sent (8 roles; text=%s voice=%s)", _send_text, _send_voice)
                except Exception as e:
                    logger.warning("Team greeting roll call failed: %s", e)
                # First interaction pack: invite Avi to reply to one role (optional, once per env)
                _first_pack_enabled = os.getenv("TG_FIRST_INTERACTION_PACK_ENABLED", "false").strip().lower() in ("true", "1", "yes")
                _first_pack_once = os.getenv("TG_FIRST_INTERACTION_PACK_ONCE", "true").strip().lower() in ("true", "1", "yes")
                if _first_pack_enabled:
                    _pack_sentinel = state_path.parent / "runtime" / "telegram" / ".first_interaction_pack_sent"
                    if _first_pack_once and _pack_sentinel.exists():
                        logger.debug("First interaction pack skipped (already sent once)")
                    else:
                        try:
                            from mint_ladder_bot.integration.telegram_bot import send_first_interaction_pack
                            if send_first_interaction_pack(owner_name=None, data_dir=state_path.parent, include_voice=None):
                                if _first_pack_once:
                                    _pack_sentinel.parent.mkdir(parents=True, exist_ok=True)
                                    _pack_sentinel.write_text("", encoding="utf-8")
                        except Exception as ep:
                            logger.warning("First interaction pack failed: %s", ep)
    except Exception as e:
        logger.debug("Telegram bot startup skipped or failed: %s", e)

    tradable_mints, bootstrap_pending_mints = _filter_tradable_and_bootstrap_mints(
        status_data, state
    )
    tradable_set = {m.mint for m in tradable_mints}
    status_by_mint = {m.mint: m for m in status_data.mints}
    for mint_addr, ms in state.mints.items():
        sm = status_by_mint.get(mint_addr)
        entry_price = getattr(ms, "entry_price_sol_per_token", 0) or 0
        if sm and getattr(sm.entry, "entry_price_sol_per_token", None) is not None:
            entry_price = sm.entry.entry_price_sol_per_token
        entry_source = (getattr(sm.entry, "entry_source", None) or "unknown") if sm and sm.entry else "unknown"
        ms.entry_resolution_source = entry_source
        valid = validate_entry_price(entry_price)
        if entry_price <= 0 or entry_source == "unknown":
            ms.entry_validation_status = "invalid" if entry_price <= 0 else "unknown"
        else:
            ms.entry_validation_status = "valid" if valid else "invalid"
        ms.tradable = mint_addr in tradable_set
        if ms.tradable:
            ms.tradable_reason = "entry_valid"
        elif entry_source == "unknown":
            ms.tradable_reason = "entry_unknown"
        elif entry_price <= 0:
            ms.tradable_reason = "entry_invalid_zero"
        else:
            ms.tradable_reason = "entry_invalid_or_bootstrap_pending"

    if not tradable_mints and not bootstrap_pending_mints:
        logger.warning("No tradable or bootstrap-pending mints found in status file; idle mode (no cycles will run until status has mints).")
        # Keep process alive so container stays healthy; main loop below will still run but with empty tradable set.
        # Skip further startup initialization.
        tradable_mints = []
        bootstrap_pending_mints = []

    for m in tradable_mints + bootstrap_pending_mints:
        trading_bag_raw, moonbag_raw = compute_trading_bag(
            balance_raw=m.balance_raw,
            trading_bag_pct=config.trading_bag_pct,
        )
        ensure_mint_state(
            state=state,
            mint=m.mint,
            entry_price_sol_per_token=m.entry.entry_price_sol_per_token,
            trading_bag_raw=trading_bag_raw,
            moonbag_raw=moonbag_raw,
            entry_source=m.entry.entry_source if m.entry.entry_source != "unknown" else None,
        )

    # Market entry bootstrap: set entry from DexScreener price for unknown-entry mints (no buy, no status overwrite).
    now_utc = datetime.now(tz=timezone.utc)
    for m in bootstrap_pending_mints:
        if m.entry.entry_price_sol_per_token > 0 and getattr(m.entry, "entry_source", "unknown") != "unknown":
            continue
        ms = state.mints.get(m.mint)
        if ms is None:
            continue
        if validate_entry_price(ms.entry_price_sol_per_token):
            continue
        ds = m.market.dexscreener if m.market else None
        price_native = ds.price_native if ds is not None else None
        if price_native is not None:
            try:
                price_native = float(price_native)
            except (TypeError, ValueError):
                price_native = None
        if price_native is None or price_native <= 0:
            logger.warning(
                "Mint skipped: market price unavailable (mint=%s symbol=%s)",
                m.mint,
                m.symbol or "",
            )
            continue
        if not validate_entry_price(price_native):
            logger.warning(
                "Mint skipped: market price out of range (mint=%s price_native=%.6e)",
                m.mint,
                price_native,
            )
            continue
        ms.entry_price_sol_per_token = price_native
        ms.original_entry_price_sol_per_token = price_native
        ms.working_entry_price_sol_per_token = price_native
        ms.entry_source = "market_bootstrap"
        ms.bootstrap_from_market = True
        ms.bootstrap_timestamp = now_utc
        if getattr(ms, "bootstrap", None) is None:
            ms.bootstrap = BootstrapInfo()
        ms.bootstrap.bootstrap_pending = False
        ms.bootstrap.bootstrap_completed_at = now_utc
        logger.info(
            "BOOTSTRAP entry mint=%s source=market price=%.6e",
            m.mint,
            price_native,
        )
        logger.info(
            "Bootstrap entry set from market price: mint=%s price=%s",
            m.mint,
            price_native,
        )
    tradable_mints, bootstrap_pending_mints = _filter_tradable_and_bootstrap_mints(
        status_data, state
    )
    for m in tradable_mints + bootstrap_pending_mints:
        ms = state.mints.get(m.mint)
        if ms is not None and getattr(ms, "last_known_balance_raw", None) is None:
            ms.last_known_balance_raw = m.balance_raw
    clean_start = os.getenv("CLEAN_START", "").strip().lower() in ("1", "true", "yes")
    if clean_start:
        logger.warning("CLEAN_START=1: skipping lot migration, tx-first, backfill, and startup buy detection (empty active lots).")
    # Tx-first BEFORE lot migration: create tx-derived lots from wallet txs first; only then fill mints that still have no lots with bootstrap_snapshot (avoids double-count and bootstrap dominance).
    rpc_startup = RpcClient(config.rpc_endpoint, timeout_s=config.rpc_timeout_s, max_retries=config.max_retries)
    decimals_by_mint_startup = {m.mint: (getattr(m, "decimals", None) or 6) for m in status_data.mints}
    full_ledger = os.getenv("TX_FULL_LEDGER", "").strip().lower() in ("1", "true", "yes")
    max_sigs = ENTRY_SCAN_MAX_SIGNATURES if full_ledger else min(max(20, getattr(config, "entry_infer_signature_limit", 60) or 60), ENTRY_SCAN_MAX_SIGNATURES)
    if full_ledger:
        logger.info("TX_FULL_LEDGER=1: scanning up to %s signatures for tx-derived ledger", max_sigs)
    symbol_by_mint_startup = {m.mint: (m.symbol or m.mint[:8]) for m in status_data.mints}
    if not clean_start:
        _ = run_tx_first_lot_engine(
            state, rpc_startup, pubkey, decimals_by_mint_startup,
            journal_path=event_journal_path, max_signatures=max_sigs,
            symbol_by_mint=symbol_by_mint_startup,
        )
    # Lot migration: only for mints that still have no lots after tx-first (bootstrap_snapshot only when no tx-derived lot exists).
    if not clean_start:
        _ensure_lots_migrated(state)
    # Deterministic lot-source summary: never double-count; bootstrap only where no tx-derived lot.
    if not clean_start:
        n_tx_derived = sum(1 for ms in state.mints.values() for lot in (getattr(ms, "lots", None) or []) if getattr(lot, "source", None) in ("tx_exact", "tx_parsed"))
        n_bootstrap = sum(1 for ms in state.mints.values() for lot in (getattr(ms, "lots", None) or []) if getattr(lot, "source", None) == "bootstrap_snapshot")
        logger.info("LOT_SOURCE_STARTUP_SUMMARY tx_derived_lots=%d bootstrap_snapshot_lots=%d (bootstrap only where no tx-derived)", n_tx_derived, n_bootstrap)
    # Optional one-off backfill: run tx-first with larger signature window once per state dir.
    backfill_once = not clean_start and os.getenv("TX_BACKFILL_ONCE", "").strip().lower() in ("1", "true", "yes")
    backfill_sentinel = state_path.parent / ".tx_backfill_done"
    if backfill_once and not backfill_sentinel.exists():
        backfill_limit = min(max(100, int(os.getenv("TX_BACKFILL_SIGNATURES", "200"))), 500)
        n_backfill = run_tx_first_lot_engine(
            state, rpc_startup, pubkey, decimals_by_mint_startup,
            journal_path=event_journal_path, max_signatures=backfill_limit,
            symbol_by_mint=symbol_by_mint_startup,
        )
        try:
            backfill_sentinel.write_text(datetime.now(tz=timezone.utc).isoformat())
        except Exception as e:
            logger.warning("Could not write backfill sentinel: %s", e)
        logger.info("TX_BACKFILL_ONCE completed: %d lots created (max_sigs=%d)", n_backfill, backfill_limit)
    # Run buy detection (reconciliation + unmatched balance increase fallback).
    if not clean_start:
        _run_buy_detection(
            state,
            {m.mint: int(m.balance_raw) for m in status_data.mints},
            config,
            rpc=rpc_startup,
            safety_path=safety_path,
            journal_path=event_journal_path,
            wallet_pubkey=pubkey,
            project_root=state_path.parent,
            symbol_by_mint={m.mint: (m.symbol or m.mint[:8]) for m in status_data.mints},
            decimals_by_mint=decimals_by_mint_startup,
        )
        n_ext_startup = _ingest_external_sells(
            state, rpc_startup, pubkey, max_signatures=min(500, ENTRY_SCAN_MAX_SIGNATURES * 2),
            journal_path=event_journal_path,
        )
        if n_ext_startup > 0:
            logger.info("Startup: EXTERNAL_SELL_INGEST ingested=%d (ledger updated)", n_ext_startup)
    n_display_pending_downgraded = _downgrade_display_pending_lots(state)
    if n_display_pending_downgraded:
        logger.info("Startup: downgraded %d display-pending lot(s) (snapshot+non-initial_migration) to unknown", n_display_pending_downgraded)
    n_downgraded = _downgrade_invalid_exact_lots(state)
    if n_downgraded:
        logger.info("Startup: downgraded %d tx_exact lot(s) with invalid entry price to unknown", n_downgraded)
    n_resolved = _resolve_pending_price_lots(
        state, rpc_startup, pubkey, config, decimals_by_mint_startup, event_journal_path,
    )
    if n_resolved:
        logger.info("Startup: resolved %d pending_price_resolution lots", n_resolved)
    rpc_startup.close()
    save_state_atomic(state_path, state)

    # Lot-source observability: structured summary and per-token summary for "why no trades?" debugging.
    from . import dashboard_truth as dt
    try:
        state_dict = state.model_dump()
        summary = dt.global_lot_source_summary(state_dict)
        logger.info(
            "LOT_SOURCE_STARTUP_SUMMARY total_tokens=%d total_lots=%d tx_exact=%d tx_parsed=%d bootstrap_snapshot=%d transfer_received_unknown=%d pending=%d",
            summary["total_tokens"],
            summary["total_lots"],
            summary["tx_exact"],
            summary["tx_parsed"],
            summary["bootstrap_snapshot"],
            summary["transfer_received_unknown"],
            summary["pending_lots_count"],
        )
        status_by_mint = {m.mint: m.model_dump() for m in status_data.mints}
        for mint_addr, ms in state.mints.items():
            sm = status_by_mint.get(mint_addr) or {}
            sold_raw = sum(int(getattr(s, "sold_raw", 0) or 0) for s in (ms.executed_steps or {}).values())
            truth = dt.token_truth(
                mint_addr,
                ms.model_dump(),
                sm,
                decimals=getattr(next((m for m in status_data.mints if m.mint == mint_addr), None), "decimals", 6),
                symbol=next((m.symbol or m.mint[:8] for m in status_data.mints if m.mint == mint_addr), mint_addr[:8]),
                sold_raw_from_steps=sold_raw,
            )
            logger.info(
                "LOT_SOURCE_TOKEN mint=%s symbol=%s balance_raw=%s sellable_raw=%s lots_tx=%s lots_bootstrap=%s lots_unknown=%s position=%s has_entry=%s has_market=%s",
                mint_addr[:12],
                truth.get("symbol"),
                truth.get("balance_raw"),
                truth.get("sellable_raw"),
                truth.get("counts_by_source", {}).get("tx_exact", 0) + truth.get("counts_by_source", {}).get("tx_parsed", 0),
                truth.get("counts_by_source", {}).get("bootstrap_snapshot", 0),
                truth.get("counts_by_source", {}).get("transfer_received_unknown", 0),
                truth.get("position_status"),
                truth.get("has_entry"),
                truth.get("has_market_data"),
            )
            for alert in truth.get("alerts") or []:
                logger.warning("LOT_SOURCE_WARNING mint=%s alert=%s (balance=%s sellable=%s)", mint_addr[:12], alert, truth.get("balance_raw"), truth.get("sellable_raw"))
    except Exception as e:
        logger.debug("LOT_SOURCE_STARTUP_SUMMARY failed: %s", e)

    # Startup: populate liquidity cap per mint so we can log it.
    for m in tradable_mints:
        ms = state.mints.get(m.mint)
        if ms is not None:
            _update_liquidity_cap(m, ms)

    # Startup validation log: wallet, tradable count, run mode, paused, paths, per-mint trading bag / cooldown / liquidity cap.
    project_runtime_dir = state_path.parent
    paused_count_startup = sum(
        1
        for ms in state.mints.values()
        if getattr(ms.failures, "paused_until", None) is not None
        and ms.failures.paused_until > datetime.now(tz=timezone.utc)
    )
    startup_summary = _build_startup_summary(
        wallet=pubkey,
        run_mode="LIVE" if not monitor_only else "MONITOR_ONLY",
        project_runtime_dir=project_runtime_dir,
        state_path=state_path,
        status_path=status_path,
        tradable_mints=len(tradable_mints),
        bootstrap_pending_mints=len(bootstrap_pending_mints),
        paused_mints=paused_count_startup,
        stop_paths=_stop_file_paths(state_path),
    )
    logger.info("Startup: wallet=%s", startup_summary["wallet"])
    logger.info("Startup: tradable mints=%d", startup_summary["tradable_mints"])
    logger.info("Startup: run mode=%s", startup_summary["run_mode"])
    logger.info("Startup: project_runtime_dir=%s", startup_summary["project_runtime_dir"])
    logger.info("Startup: state file path=%s", startup_summary["state_file"])
    logger.info("Startup: status file path=%s", startup_summary["status_file"])
    logger.info(
        "Startup: tradable=%d bootstrap_pending=%d paused_mints=%d",
        startup_summary["tradable_mints"],
        startup_summary["bootstrap_pending_mints"],
        startup_summary["paused_mints"],
    )
    total_bag_tokens = 0.0
    for m in tradable_mints:
        ms = state.mints.get(m.mint)
        if ms is None:
            continue
        bag_raw = int(ms.trading_bag_raw)
        bag_tokens = bag_raw / (10 ** m.decimals)
        total_bag_tokens += bag_tokens
        cooldown_str = str(ms.cooldown_until) if ms.cooldown_until else "none"
        liq_cap = ms.liquidity_cap.max_sell_raw
        liq_str = str(liq_cap) if liq_cap is not None else "n/a"
        logger.info(
            "Startup: mint %s trading_bag=%.6f tokens cooldown_until=%s liquidity_cap_raw=%s",
            _pair_name(m),
            bag_tokens,
            cooldown_str,
            liq_str,
        )
    logger.info("Startup: total trading bag (sum tokens)=%.6f", total_bag_tokens)

    rpc = RpcClient(config.rpc_endpoint, timeout_s=config.rpc_timeout_s, max_retries=config.max_retries)

    per_mint_sells: Dict[str, List[Tuple[datetime, int]]] = {}
    per_mint_sells_24h: Dict[str, List[Tuple[datetime, int]]] = {}

    run_state: Dict[str, Any] = {
        "global_trading_paused_until": None,
        "rpc_failures_consecutive": 0,
        "cycle_mismatch_first_detected_at_cycle": None,  # cycle number when uncorrectable state/dashboard mismatch first seen; cleared on self-correction
    }

    # Startup dry validation: Jupiter quote + summary; mark failed mints paused.
    _run_startup_dry_validation(
        tradable_mints=tradable_mints,
        state=state,
        config=config,
        rpc=rpc,
    )

    # Startup warning banner
    trading_ok = getattr(config, "trading_enabled", False)
    if not trading_ok:
        mode = "SAFE MODE — TRADING DISABLED (TRADING_ENABLED != true); no execution"
    elif monitor_only:
        mode = "MONITOR ONLY (no swaps will be sent)"
    else:
        mode = "LIVE TRADING ENABLED"
    logger.warning("=== %s ===", mode)
    logger.warning("TRADING_ENABLED=%s", str(trading_ok).lower())
    logger.warning("wallet=%s", pubkey)
    logger.warning("tradable_mints=%d bootstrap_pending=%d", len(tradable_mints), len(bootstrap_pending_mints))
    logger.warning("STOP file active=%s paths_checked=%s", str(_stop_file_present(state_path)).lower(), _stop_file_paths(state_path))
    logger.warning("project_runtime_dir=%s", str(project_runtime_dir))
    logger.warning("state_file=%s", str(state_path.resolve()))
    logger.warning("status_file=%s", str(status_path.resolve()))
    logger.warning("================================")

    # Bootstrap health_status.json so dashboard never fails on first load.
    runner_mode = (
        "blocked" if not trading_ok
        else "monitor_only" if monitor_only
        else "live"
    )
    write_health_status(
        state_path.parent,
        state,
        {
            "cycles": 0,
            "rpc_latency_ms": 0.0,
            "errors": [],
            "paused_mints": 0,
            "runner_mode": runner_mode,
            "process_state": "starting",
            "last_successful_cycle_at": None,
            "last_failed_cycle_at": None,
            "current_cycle_number": 0,
            "last_error": None,
        },
    )

    # Running totals across the entire run (all cycles).
    total_sells_executed = 0
    total_sells_failed = 0
    total_buybacks_executed = 0
    total_buybacks_failed = 0

    try:
        cycle = 0
        while True:
            cycle += 1
            # One-off backfill: enrich snapshot lots with tx and real buy price when enabled.
            if cycle == 1 and not clean_start and getattr(config, "backfill_lot_tx_once", False):
                backfill_done = state_path.parent / "lot_tx_backfill_done"
                if not backfill_done.exists():
                    try:
                        n = _backfill_lot_tx(state, pubkey, rpc, config, event_journal_path)
                        if n > 0:
                            save_state_atomic(state_path, state)
                        backfill_done.write_text("")
                        if n > 0:
                            logger.info("Backfill lot tx: %d lots enriched", n)
                    except Exception as e:
                        logger.warning("Backfill lot tx failed: %s", e)

            cycle_t0 = time.monotonic()
            sells_executed = 0
            sells_failed = 0
            buybacks_executed = 0
            buybacks_failed = 0
            paused_mints = 0
            liquidity_skips = 0
            no_step = 0
            price_none = 0
            below_target = 0
            hour_cap_skips = 0
            min_trade_skips = 0
            sell_readiness: Dict[str, Dict[str, Any]] = {}

            now_utc = datetime.now(tz=timezone.utc)
            stop_active = _stop_file_present(state_path)
            if stop_active:
                logger.warning("STOP file present; trading disabled this cycle (no sells, no buybacks, no bootstrap swaps — monitoring only).")
            global_pause_until = run_state.get("global_trading_paused_until")
            if global_pause_until is not None and now_utc < global_pause_until:
                logger.warning(
                    "Global trading paused due to RPC instability until %s.",
                    global_pause_until,
                )
            # Execution allowed only when TRADING_ENABLED=true and LIVE_TRADING=true (CEO directive: monitor default),
            # STOP is not active, trading is not env-disabled, and we are not in global RPC cooldown.
            trading_disabled = _compute_trading_disabled(
                config=config,
                stop_active=stop_active,
                global_pause_until=global_pause_until,
                now_utc=now_utc,
            )

            # Live validation: discover new mints from wallet (e.g. manual Pump.fun buy)
            from .status_snapshot import discover_new_mints
            existing_mints = {m.mint for m in status_data.mints}
            new_mints_list = discover_new_mints(pubkey, rpc, config, existing_mints)
            for m in new_mints_list:
                logger.info("WALLET_MINT_DISCOVERED mint=%s amount_raw=%s token_account=%s", m.mint[:12], m.balance_raw, (m.token_account[:8] if getattr(m, "token_account", None) else "n/a"))
                status_data.mints.append(m)
                trading_bag_raw, moonbag_raw = compute_trading_bag(
                    balance_raw=m.balance_raw,
                    trading_bag_pct=config.trading_bag_pct,
                )
                ensure_mint_state(
                    state=state,
                    mint=m.mint,
                    entry_price_sol_per_token=m.entry.entry_price_sol_per_token or 0.0,
                    trading_bag_raw=trading_bag_raw,
                    moonbag_raw=moonbag_raw,
                    entry_source=m.entry.entry_source if m.entry.entry_source != "unknown" else None,
                )
                ms = state.mints.get(m.mint)
                if ms is not None:
                    ms.last_known_balance_raw = m.balance_raw
                    quarantine_sec = getattr(config, "quarantine_duration_sec", 60.0) or 60.0
                    ms.protection_state = "quarantine"
                    ms.quarantine_until = now_utc + timedelta(seconds=quarantine_sec)
                    entry_p = getattr(ms, "entry_price_sol_per_token", None) or 0.0  # for event/log only; lot gets its own
                    if not getattr(ms, "lots", None):
                        lot_source: str = "tx_exact"
                        lot_entry_conf: str = "exact"
                        lot_conf = "known"
                        tx_sig_new: Optional[str] = None
                        tx_price_new: Optional[float] = None
                        tx_when: Optional[datetime] = None
                        try:
                            amount_raw_int = int(m.balance_raw)
                            if rpc and amount_raw_int > 0:
                                from .tx_infer import find_buy_tx_for_delta
                                max_sigs = min(max(20, getattr(config, "entry_infer_signature_limit", 60) or 60), ENTRY_SCAN_MAX_SIGNATURES)
                                dec = getattr(m, "decimals", None) or 6
                                buy_tx = find_buy_tx_for_delta(pubkey, m.mint, amount_raw_int, rpc, max_signatures=max_sigs, decimals=dec)
                                if buy_tx:
                                    tx_sig_new, tx_price_new, tx_when = buy_tx
                                    if tx_price_new is not None and validate_entry_price(tx_price_new):
                                        ms.entry_price_sol_per_token = tx_price_new
                                        ms.original_entry_price_sol_per_token = tx_price_new
                                        ms.working_entry_price_sol_per_token = tx_price_new
                                        ms.entry_source = "inferred_from_tx"
                                    else:
                                        if tx_price_new is not None and event_journal_path:
                                            append_event(event_journal_path, "PRICE_SANITY_REJECTED", {"mint": m.mint[:12], "tx_sig": (tx_sig_new[:16] if tx_sig_new else None), "price": tx_price_new, "reason": "outside ENTRY_PRICE bounds"})
                                        logger.warning("PRICE_SANITY_REJECTED mint=%s tx_price=%.6e (new mint); entry=null confidence=unknown", m.mint[:12], tx_price_new or 0)
                                        tx_sig_new, tx_price_new, tx_when = None, None, None
                                        lot_source = "tx_exact"
                                        lot_entry_conf = "unknown"
                                        lot_conf = "unknown"
                                else:
                                    logger.warning("BALANCE_DELTA_WITHOUT_TX mint=%s amount_raw=%s (new mint; no matching tx — no lot created)", m.mint[:12], amount_raw_int)
                                    if event_journal_path:
                                        append_event(event_journal_path, "TX_LOOKUP_FAILED", {"mint": m.mint[:12], "delta_raw": amount_raw_int, "reason": "no_matching_tx"})
                                        append_event(event_journal_path, "BALANCE_DELTA_WITHOUT_TX", {"mint": m.mint[:12], "amount_raw": amount_raw_int, "reason": "new_mint_no_tx"})
                                    tx_sig_new, tx_price_new, tx_when = None, None, None
                                    lot_source = ""
                                    lot_entry_conf = "unknown"
                                    lot_conf = "unknown"
                        except (ValueError, TypeError):
                            lot_source = ""
                            lot_entry_conf = "unknown"
                            lot_conf = "unknown"
                        entry_p = tx_price_new if (tx_price_new is not None and tx_price_new > 0) else entry_p
                        # Lot creation only from parsed tx. If no tx found, do not create lot (tx-only policy).
                        if lot_source == "tx_exact" and tx_sig_new:
                            lot_entry_p = tx_price_new if (tx_price_new is not None and tx_price_new > 0) else None
                            lot = LotInfo.create(
                                m.mint,
                                trading_bag_raw,
                                entry_price=lot_entry_p,
                                confidence=lot_conf,  # type: ignore[arg-type]
                                source=lot_source,
                                entry_confidence=lot_entry_conf,  # type: ignore[arg-type]
                                tx_signature=tx_sig_new,
                                detected_at=tx_when,
                            )
                            ms.lots = [lot]
                            entry_p = lot_entry_p if lot_entry_p is not None else entry_p
                            if event_journal_path:
                                append_event(event_journal_path, EVENT_LOT_CREATED, {"mint": m.mint[:12], "lot_id": lot.lot_id[:8], "token_amount_raw": trading_bag_raw, "entry_confidence": lot_entry_conf, "source": lot_source})
                                append_event(event_journal_path, EVENT_LOT_CREATED_TX_EXACT, {"mint": m.mint[:12], "lot_id": lot.lot_id[:8], "tx_signature": (tx_sig_new[:16] if tx_sig_new else None)})
                        else:
                            if event_journal_path:
                                append_event(event_journal_path, "MINT_DISCOVERED_NO_LOT", {"mint": m.mint[:12], "amount_raw": m.balance_raw, "reason": "no_tx_found_tx_only_policy"})
                    n_lots = len(getattr(ms, "lots", None) or [])
                    if event_journal_path:
                        append_event(event_journal_path, EVENT_MINT_DETECTED, {"mint": m.mint[:12], "amount_raw": m.balance_raw, "entry_estimated": entry_p, "quarantine_sec": quarantine_sec, "lots_created": n_lots})
                    logger.info("MINT_DETECTED mint=%s amount_raw=%s source=discover_new_mints lot_created=%s (tx-only)", m.mint[:12], m.balance_raw, n_lots)
                    logger.info(
                        "MINT_DETECTED mint=%s amount_raw=%s entry_estimated=%.6e protection_plan=ladder+stop_loss",
                        m.mint[:12], m.balance_raw, ms.entry_price_sol_per_token or 0,
                    )
                    logger.info("PROTECTION_PLAN_CREATED mint=%s lots=%s trading_bag_raw=%s (quarantine %.0fs)", m.mint[:12], n_lots, trading_bag_raw, quarantine_sec)
            if new_mints_list:
                try:
                    write_status_synced(status_data, state, status_path)
                    logger.info("Status persisted with %d new mints (total mints=%d)", len(new_mints_list), len(status_data.mints))
                except Exception as e:
                    logger.warning("Failed to persist status after new mints: %s", e)

            # Sniper Phase 1: resolve pending attempts and process manual-seed queue.
            try:
                sniper_service.resolve_pending_attempts()
            except Exception as e:
                logger.warning("Sniper resolve_pending_attempts failed: %s", e)
            try:
                sniper_service.process_candidate_queue()
            except Exception as e:
                logger.warning("Sniper process_candidate_queue failed: %s", e)

            tradable_mints, bootstrap_pending_mints = _filter_tradable_and_bootstrap_mints(
                status_data, state
            )
            all_tracked = tradable_mints + bootstrap_pending_mints
            decimals_by_mint_cycle = {m.mint: (getattr(m, "decimals", None) or 6) for m in all_tracked}
            logger.info("Cycle %d: refreshing balances for %d mints", cycle, len(all_tracked))
            balances_refresh: Dict[str, int] = {}
            for mint_status in all_tracked:
                mint_state = state.mints.get(mint_status.mint)
                if mint_state is None:
                    continue
                try:
                    value = rpc.get_token_account_balance_quick(mint_status.token_account, timeout_s=5.0)
                    if value and "amount" in value:
                        amount_raw = value.get("amount")
                        if amount_raw is not None:
                            balance_raw = int(amount_raw)
                            if balance_raw >= 0:
                                balances_refresh[mint_status.mint] = balance_raw
                                trading_bag_raw, moonbag_raw = compute_trading_bag(
                                    balance_raw=str(balance_raw),
                                    trading_bag_pct=config.trading_bag_pct,
                                )
                                mint_state.trading_bag_raw = str(trading_bag_raw)
                                mint_state.moonbag_raw = str(moonbag_raw)
                except Exception as exc:
                    logger.debug("Refresh balance for %s failed: %s", _pair_name(mint_status), exc)
            if balances_refresh and not clean_start:
                # Tx-first: create lots from wallet txs before balance-delta reconciliation.
                max_sigs_cycle = min(max(50, getattr(config, "entry_infer_signature_limit", 100) or 100), ENTRY_SCAN_MAX_SIGNATURES)
                symbol_by_mint_cycle = {m.mint: (m.symbol or m.mint[:8]) for m in all_tracked}
                _ = run_tx_first_lot_engine(
                    state, rpc, pubkey, decimals_by_mint_cycle,
                    journal_path=event_journal_path, max_signatures=max_sigs_cycle,
                    symbol_by_mint=symbol_by_mint_cycle,
                )
                n_external = _ingest_external_sells(
                    state, rpc, pubkey, max_signatures=min(200, ENTRY_SCAN_MAX_SIGNATURES * 2),
                    journal_path=event_journal_path,
                )
                if n_external > 0:
                    logger.info("EXTERNAL_SELL_INGEST cycle=%d ingested=%d (ledger updated)", cycle, n_external)
                _run_buy_detection(
                    state,
                    balances_refresh,
                    config,
                    rpc=rpc,
                    safety_path=safety_path,
                    journal_path=event_journal_path,
                    wallet_pubkey=pubkey,
                    project_root=event_journal_path.parent if event_journal_path else None,
                    symbol_by_mint={m.mint: (m.symbol or m.mint[:8]) for m in all_tracked},
                    decimals_by_mint=decimals_by_mint_cycle,
                )
            # Resolver: run every cycle so pending_price_resolution lots get resolved or downgraded even when balance refresh failed.
            n_resolved = _resolve_pending_price_lots(
                state, rpc, pubkey, config, decimals_by_mint_cycle, event_journal_path,
            )
            if n_resolved > 0 and event_journal_path:
                _notify_founder(event_journal_path.parent, f"Resolver: {n_resolved} pending lot(s) resolved to exact.", "Mint Ladder", critical=False)
            # Downgrade display-pending (snapshot + non-initial_migration) to unknown every cycle so they do not accumulate; dashboard shows unknown, not pending.
            n_display_pending = _downgrade_display_pending_lots(state)
            if n_display_pending > 0:
                logger.info("Cycle %d: downgraded %d display-pending lot(s) to unknown", cycle, n_display_pending)
            # Integrity: wallet_balance == sum(active_lots). Reconciliation model: explain holdings.
            for mint, actual_raw in balances_refresh.items():
                ms = state.mints.get(mint)
                if ms is None:
                    continue
                expl = _compute_mint_holding_explanation(ms)
                sum_lots = expl["sum_active_lots"]
                tx_proven_raw = sum_lots
                wallet_balance_raw = actual_raw
                external_excess_raw = 0
                mismatch_mode: Optional[str] = None
                if wallet_balance_raw >= tx_proven_raw > 0:
                    # Same-mint external excess: preserve tx-proven core, quarantine only the excess.
                    external_excess_raw = wallet_balance_raw - tx_proven_raw
                    if external_excess_raw > 0:
                        mismatch_mode = "external_excess"
                elif tx_proven_raw > wallet_balance_raw:
                    mismatch_mode = "underwater"

                if sum_lots != wallet_balance_raw:
                    unmatched_raw = wallet_balance_raw - sum_lots
                    logger.warning(
                        "STATE_BALANCE_MISMATCH mint=%s wallet_balance=%s sum_active_lots=%s external_excess_raw=%s mismatch_mode=%s (do not fabricate lots)",
                        mint[:12],
                        wallet_balance_raw,
                        sum_lots,
                        external_excess_raw if external_excess_raw > 0 else 0,
                        mismatch_mode or "",
                    )
                    if event_journal_path:
                        payload = {
                            "mint": mint[:12],
                            "wallet_balance": wallet_balance_raw,
                            "sum_active_lots": sum_lots,
                        }
                        if external_excess_raw > 0:
                            payload["external_excess_raw"] = external_excess_raw
                        if mismatch_mode:
                            payload["mismatch_mode"] = mismatch_mode
                        append_event(event_journal_path, "STATE_BALANCE_MISMATCH", payload)
                        append_event(
                            event_journal_path,
                            UNEXPLAINED_WALLET_CHANGE,
                            {
                                "mint": mint[:12],
                                "wallet_balance": wallet_balance_raw,
                                "sum_active_lots": sum_lots,
                                "unmatched_raw_delta": unmatched_raw,
                            },
                        )

                # Update per-mint reconciliation / pause state. For external_excess mode we
                # keep tx-proven core tradable and quarantine only the excess (no pause).
                ms.external_excess_raw = str(external_excess_raw) if external_excess_raw > 0 else None
                ms.reconciliation_mode = mismatch_mode
                if mismatch_mode != "external_excess":
                    _update_reconciliation_pause_for_mint(
                        mint=mint,
                        mint_state=ms,
                        actual_raw=wallet_balance_raw,
                        sum_lots=sum_lots,
                        now=now_utc,
                        config=config,
                        event_journal_path=event_journal_path,
                    )

                logger.info(
                    "MINT_HOLDING_EXPLANATION mint=%s wallet=%s tx_derived=%s bootstrap=%s transfer_unknown=%s sold=%s",
                    mint[:12],
                    wallet_balance_raw,
                    expl["tx_derived_raw"],
                    expl["bootstrap_snapshot_raw"],
                    expl["transfer_unknown_raw"],
                    expl["sold_raw"],
                )
                if event_journal_path and (sum_lots != wallet_balance_raw or cycle % 10 == 1):
                    payload = {
                        "mint": mint[:12],
                        "wallet_balance": wallet_balance_raw,
                        **expl,
                    }
                    if external_excess_raw > 0:
                        payload["external_excess_raw"] = external_excess_raw
                    if mismatch_mode:
                        payload["reconciliation_mode"] = mismatch_mode
                    append_event(event_journal_path, "MINT_HOLDING_EXPLANATION", payload)
                sum_lots_tx = _trading_bag_from_lots(ms)
                if sum_lots_tx > actual_raw:
                    logger.warning(
                        "RECONCILIATION_WARNING mint=%s sum_lots_tx=%s actual_balance=%s; capping sellable to actual (no lot fabrication).",
                        mint[:12], sum_lots_tx, actual_raw,
                    )
                    if event_journal_path:
                        append_event(event_journal_path, EVENT_RECONCILED, {"mint": mint[:12], "sum_lots_raw": sum_lots_tx, "actual_raw": actual_raw})
                    trading_bag_raw, moonbag_raw = compute_trading_bag(str(actual_raw), config.trading_bag_pct)
                    ms.trading_bag_raw = str(trading_bag_raw)
                    ms.moonbag_raw = str(moonbag_raw)
                # Manual override policy: evaluate reconciliation bypass and effective override bag.
                try:
                    effective_override = _evaluate_manual_override_bypass(
                        mint=mint,
                        mint_state=ms,
                        actual_raw=actual_raw,
                        config=config,
                        event_journal_path=event_journal_path,
                    )
                    # When bypass is NOT active, retain existing combined tx+override bag semantics.
                    if not getattr(ms, "manual_override_bypass_active", False):
                        if getattr(config, "enable_manual_override_inventory", False):
                            allowed = set(getattr(config, "manual_override_allowed_mints", []) or [])
                            if mint in allowed:
                                _update_trading_bag_with_override(
                                    mint_state=ms,
                                    config=config,
                                    mint_addr=mint,
                                    wallet_balance_raw=actual_raw,
                                )
                    else:
                        # Bypass active: only manual-override inventory is considered tradable.
                        ms.trading_bag_raw = str(effective_override if effective_override > 0 else 0)
                except Exception:
                    # Never let override computation break core reconciliation / pause logic.
                    pass

            # Per-cycle reconciliation second pass: remove any stale invalid-exact or display-pending so state/dashboard stay in sync without restart.
            n_invalid_2, n_display_2 = _run_cycle_reconciliation_second_pass(state)
            if n_invalid_2 > 0 or n_display_2 > 0:
                logger.warning(
                    "CYCLE_STATE_MISMATCH cycle=%d second_pass_fixed invalid_exact=%d display_pending=%d (corrected in-process)",
                    cycle, n_invalid_2, n_display_2,
                )
                run_state["cycle_mismatch_first_detected_at_cycle"] = None
                if event_journal_path:
                    _notify_founder(
                        event_journal_path.parent,
                        "Cycle self-correction applied — state/dashboard mismatch fixed without restart.",
                        "Mint Ladder",
                        critical=False,
                    )

            for mint_status in bootstrap_pending_mints:
                if getattr(config, "live_protection_only", False):
                    continue
                mint_state = state.mints.get(mint_status.mint)
                if mint_state is None:
                    continue
                result = _run_bootstrap_buy(
                    mint_status=mint_status,
                    mint_state=mint_state,
                    state=state,
                    state_path=state_path,
                    rpc=rpc,
                    config=config,
                    pubkey=pubkey,
                    sign_tx=sign_tx,
                    trading_disabled=trading_disabled,
                    monitor_only=monitor_only,
                )
                if result == "executed":
                    tradable_mints, bootstrap_pending_mints = _filter_tradable_and_bootstrap_mints(
                        status_data, state
                    )

            logger.info("Cycle %d: evaluating %d mints", cycle, len(tradable_mints))
            cycle_prices: Dict[str, float] = {}
            for mint_status in tradable_mints:
                mint = mint_status.mint
                mint_state = state.mints.get(mint)
                if mint_state is None:
                    continue
                # Quarantine: skip protection until quarantine_until passes
                protection_state = getattr(mint_state, "protection_state", "active")
                quarantine_until = getattr(mint_state, "quarantine_until", None)
                if protection_state == "quarantine" and quarantine_until is not None:
                    if now_utc < quarantine_until:
                        logger.info("PROTECTION_ONLY_BLOCK mint=%s symbol=%s reason=quarantine until=%s", mint, mint_status.symbol or mint[:8], quarantine_until)
                        continue
                    mint_state.protection_state = "active"
                    mint_state.quarantine_until = None
                    if event_journal_path:
                        append_event(event_journal_path, EVENT_PROTECTION_ARMED, {"mint": mint[:12]})

                _check_liquidity_collapse(mint_status, mint_state, config)
                # Compute wallet balance and current bag for observability.
                try:
                    wallet_balance_raw = int(mint_status.balance_raw)
                except (ValueError, TypeError):
                    wallet_balance_raw = 0
                try:
                    runtime_bag_raw = int(mint_state.trading_bag_raw)
                except (ValueError, TypeError):
                    runtime_bag_raw = 0
                paused_for_recon = (
                    _is_paused(mint_state)
                    and getattr(getattr(mint_state, "failures", None), "last_error", "") == "reconciliation_mismatch"
                )
                bypass_active = getattr(mint_state, "manual_override_bypass_active", False)
                try:
                    override_tradable_raw = int(mint_state.manual_override_tradable_raw or 0)
                except (TypeError, ValueError):
                    override_tradable_raw = 0
                # Normal pause: block sells entirely unless reconciliation bypass is active with override inventory.
                if _is_paused(mint_state) and not (paused_for_recon and bypass_active and override_tradable_raw > 0):
                    paused_mints += 1
                    logger.info("Mint %s is paused until %s", _pair_name(mint_status), mint_state.failures.paused_until)
                    # Surface pause in readiness map so dashboard / API can explain why no sells.
                    reason = getattr(mint_state.failures, "last_error", "") or "paused"
                    bag_zero_reason = None
                    if wallet_balance_raw > 0 and runtime_bag_raw == 0:
                        bag_zero_reason = classify_bag_zero_reason(mint_state.dict(), wallet_balance_raw)
                        logger.info(
                            "NON_TRADABLE_BAG_ZERO mint=%s symbol=%s balance_raw=%s bag_raw=%s reason=%s",
                            mint[:12], mint_status.symbol or mint[:8], wallet_balance_raw, runtime_bag_raw, bag_zero_reason,
                        )
                    sell_readiness[mint] = {
                        "ready": False,
                        "reason": "paused",
                        "sell_blocked_reason": "bag_zero" if bag_zero_reason else reason,
                        "bag_zero_reason": bag_zero_reason,
                    }
                    continue

                # Non-tradable inventory: wallet has balance but bag is zero (e.g. unknown-entry lots).
                if wallet_balance_raw > 0 and runtime_bag_raw == 0:
                    bag_zero_reason = classify_bag_zero_reason(mint_state.dict(), wallet_balance_raw)
                    logger.info(
                        "NON_TRADABLE_BAG_ZERO mint=%s symbol=%s balance_raw=%s bag_raw=%s reason=%s",
                        mint[:12], mint_status.symbol or mint[:8], wallet_balance_raw, runtime_bag_raw, bag_zero_reason,
                    )
                    sell_readiness[mint] = {
                        "ready": False,
                        "reason": "bag_zero",
                        "sell_blocked_reason": "bag_zero",
                        "bag_zero_reason": bag_zero_reason,
                    }
                    continue

                if not _enforce_liquidity_guard(mint_status, config):
                    liquidity_skips += 1
                    logger.info("LIQUIDITY_BLOCK mint=%s symbol=%s", mint, mint_status.symbol or mint[:8])
                    continue

                # Get current price in SOL/token and update dynamic analytics.
                current_price = _get_current_price_sol(
                    mint_status=mint_status,
                    runtime_state=mint_state,
                    rpc=rpc,
                    config=config,
                    status_created_at=status_data.created_at,
                )
                # Compact status line: time pair-name current_price next-step-ladder
                now = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
                price_str = f"{current_price:.2e}" if current_price is not None else "-"
                if current_price is None:
                    price_none += 1
                    logger.info("QUOTE_UNAVAILABLE mint=%s symbol=%s reason=price_none", mint, mint_status.symbol or mint[:8])
                    continue
                cycle_prices[mint] = current_price
                entry = _working_entry(mint_state)
                stop_loss_pct = getattr(config, "stop_loss_pct", 0.15) or 0.15
                break_even_enabled = getattr(config, "break_even_enabled", False)
                break_even_trigger = getattr(config, "break_even_trigger_pct", 0.05) or 0.05
                if entry and entry > 0:
                    if break_even_enabled and not getattr(mint_state, "break_even_done", False):
                        if current_price >= entry * (1.0 + break_even_trigger):
                            mint_state.break_even_done = True
                            mint_state.working_entry_price_sol_per_token = entry
                            logger.info("BREAK_EVEN mint=%s entry=%.6e locked", _pair_name(mint_status), entry)
                    if current_price <= entry * (1.0 - stop_loss_pct):
                        logger.warning(
                            "STOP_LOSS_TRIGGERED mint=%s current=%.6e entry=%.6e pct=%.1f",
                            _pair_name(mint_status), current_price, entry, stop_loss_pct * 100,
                        )
                        if event_journal_path:
                            append_event(event_journal_path, EVENT_STOP_HIT, {"mint": mint[:12], "reason": "stop_loss", "current_price": current_price, "entry": entry})
                    elif getattr(mint_state, "break_even_done", False) and current_price < entry:
                        logger.warning(
                            "STOP_AT_ENTRY mint=%s current=%.6e entry=%.6e (break-even locked)",
                            _pair_name(mint_status), current_price, entry,
                        )
                        if event_journal_path:
                            append_event(event_journal_path, EVENT_STOP_HIT, {"mint": mint[:12], "reason": "stop_at_entry", "current_price": current_price, "entry": entry})

                _update_volatility_and_momentum(mint_status, mint_state)
                _update_liquidity_cap(mint_status, mint_state)
                pump = _compute_pump_info(mint_state, config)
                mint_state.pump = pump
                if pump.detected:
                    r1 = f"{pump.return_1m * 100:.1f}%" if pump.return_1m is not None else "-"
                    r5 = f"{pump.return_5m * 100:.1f}%" if pump.return_5m is not None else "-"
                    logger.info(
                        "PUMP mint=%s pair=%s return_1m=%s return_5m=%s",
                        mint,
                        _pair_name(mint_status),
                        r1,
                        r5,
                    )

                # Respect post-sell cooldown to avoid overtrading.
                if mint_state.cooldown_until and datetime.now(tz=timezone.utc) < mint_state.cooldown_until:
                    paused_mints += 1
                    logger.warning(
                        "Mint %s skipped: cooldown until %s (no sell this cycle).",
                        _pair_name(mint_status),
                        mint_state.cooldown_until,
                    )
                    logger.info("SELL_BLOCKED_COOLDOWN mint=%s symbol=%s until=%s", mint, mint_status.symbol or mint[:8], mint_state.cooldown_until)
                    _liq_usd = getattr(getattr(mint_status.market, "dexscreener", None), "liquidity_usd", None) if getattr(mint_status, "market", None) else None
                    sell_readiness[mint] = {
                        "symbol": mint_status.symbol or mint[:8],
                        "runtime_tradable_raw": int(mint_state.trading_bag_raw),
                        "current_price_sol_per_token": current_price,
                        "entry_price_sol_per_token": entry if entry else None,
                        "next_step_index": None,
                        "next_target_price": None,
                        "distance_to_next_target_pct": None,
                        "liquidity_usd": _liq_usd,
                        "sell_blocked_reason": "cooldown",
                        "sell_ready_now": False,
                    }
                    logger.info(
                        "SELL_READINESS mint=%s symbol=%s current=%.6e target=N/A diff_pct=N/A tradable=%s blocked=cooldown ready=false",
                        mint, mint_status.symbol or mint[:8], current_price, int(mint_state.trading_bag_raw),
                    )
                    continue

                # Volume-spike mode: activate only when pump + strong momentum + liquidity all agree.
                spike_mode = (
                    getattr(mint_state.pump, "detected", False)
                    and getattr(mint_state.momentum, "regime", "") == "strong"
                    and (mint_state.liquidity_cap.max_sell_raw or 0) > 0
                )
                if spike_mode:
                    logger.info(
                        "SPIKE_MODE mint=%s pair=%s (pump + strong momentum + liquidity)",
                        mint,
                        _pair_name(mint_status),
                    )
                # Build dynamic ladder and determine next unexecuted step.
                ctx = DynamicContext(
                    volatility_regime=mint_state.volatility.regime,
                    momentum_regime=mint_state.momentum.regime,
                    liquidity_cap_raw=mint_state.liquidity_cap.max_sell_raw,
                    spike_mode=spike_mode,
                )
                steps = build_dynamic_ladder_for_mint(mint_status, mint_state, ctx)
                next_step = _next_unexecuted_step(steps, mint_state)
                liquidity_usd = getattr(getattr(mint_status.market, "dexscreener", None), "liquidity_usd", None) if getattr(mint_status, "market", None) else None
                runtime_tradable_raw = int(mint_state.trading_bag_raw)
                if not next_step:
                    no_step += 1
                    block_reason = "empty_ladder" if not steps else "all_steps_executed"
                    if not steps:
                        logger.info("SELL_BLOCKED_NO_STEP mint=%s symbol=%s reason=empty_ladder", mint, mint_status.symbol or mint[:8])
                    else:
                        logger.info("SELL_BLOCKED_NO_STEP mint=%s symbol=%s reason=all_steps_executed steps=%d", mint, mint_status.symbol or mint[:8], len(steps))
                    sell_readiness[mint] = {
                        "symbol": mint_status.symbol or mint[:8],
                        "runtime_tradable_raw": runtime_tradable_raw,
                        "current_price_sol_per_token": current_price,
                        "entry_price_sol_per_token": entry if entry else None,
                        "next_step_index": None,
                        "next_target_price": None,
                        "distance_to_next_target_pct": None,
                        "liquidity_usd": liquidity_usd,
                        "sell_blocked_reason": block_reason,
                        "sell_ready_now": False,
                    }
                    logger.info(
                        "SELL_READINESS mint=%s symbol=%s current=%.6e target=N/A diff_pct=N/A tradable=%s blocked=%s ready=false",
                        mint, mint_status.symbol or mint[:8], current_price, runtime_tradable_raw, block_reason,
                    )
                    continue
                step, step_key = next_step
                entry_price = _working_entry(mint_state)
                target_price = step.target_price_sol_per_token
                diff_pct = ((current_price - target_price) / target_price * 100.0) if target_price and target_price > 0 else None
                _readiness = {
                    "symbol": mint_status.symbol or mint[:8],
                    "runtime_tradable_raw": runtime_tradable_raw,
                    "current_price_sol_per_token": current_price,
                    "entry_price_sol_per_token": entry_price,
                    "next_step_index": step.step_id,
                    "next_target_price": target_price,
                    "distance_to_next_target_pct": diff_pct,
                    "liquidity_usd": liquidity_usd,
                    "sell_blocked_reason": "",
                    "sell_ready_now": False,
                }

                trading_bag_raw = int(mint_state.trading_bag_raw)
                if not _within_hour_cap(
                    mint=mint,
                    trading_bag_raw=trading_bag_raw,
                    candidate_sell_raw=step.sell_amount_raw,
                    per_mint_sells=per_mint_sells,
                    config=config,
                ):
                    hour_cap_skips += 1
                    logger.warning(
                        "Mint %s step_id=%s skipped: per-hour sell cap reached.",
                        _pair_name(mint_status),
                        step_key,
                    )
                    logger.info("SELL_BLOCKED_HOUR_CAP mint=%s symbol=%s step_id=%s", mint, mint_status.symbol or mint[:8], step.step_id)
                    _readiness["sell_blocked_reason"] = "hour_cap"
                    sell_readiness[mint] = _readiness
                    logger.info(
                        "SELL_READINESS mint=%s symbol=%s current=%.6e target=%.6e diff_pct=%s tradable=%s blocked=hour_cap ready=false",
                        mint, _readiness["symbol"], current_price, target_price, f"{diff_pct:+.1f}%" if diff_pct is not None else "N/A", runtime_tradable_raw,
                    )
                    _audit_sell(
                        mint=mint,
                        symbol=mint_status.symbol,
                        step_id=step.step_id,
                        target_multiple=step.multiple,
                        entry_price=entry_price,
                        quoted_price=None,
                        sell_amount_raw=step.sell_amount_raw,
                        sell_ui=step.sell_amount_raw / (10 ** mint_status.decimals),
                        liquidity_cap_raw=mint_state.liquidity_cap.max_sell_raw,
                        cooldown_state="off",
                        action="skipped",
                        reason="hour_cap",
                    )
                    continue

                if not _within_24h_cap(
                    mint=mint,
                    trading_bag_raw=trading_bag_raw,
                    candidate_sell_raw=step.sell_amount_raw,
                    per_mint_sells_24h=per_mint_sells_24h,
                    config=config,
                ):
                    logger.warning(
                        "Mint %s step_id=%s skipped: per-24h sell cap reached.",
                        _pair_name(mint_status),
                        step_key,
                    )
                    logger.info("SELL_BLOCKED_24H_CAP mint=%s symbol=%s step_id=%s", mint, mint_status.symbol or mint[:8], step.step_id)
                    _readiness["sell_blocked_reason"] = "24h_cap"
                    sell_readiness[mint] = _readiness
                    logger.info(
                        "SELL_READINESS mint=%s symbol=%s current=%.6e target=%.6e diff_pct=%s tradable=%s blocked=24h_cap ready=false",
                        mint, _readiness["symbol"], current_price, target_price, f"{diff_pct:+.1f}%" if diff_pct is not None else "N/A", runtime_tradable_raw,
                    )
                    _audit_sell(
                        mint=mint,
                        symbol=mint_status.symbol,
                        step_id=step.step_id,
                        target_multiple=step.multiple,
                        entry_price=entry_price,
                        quoted_price=None,
                        sell_amount_raw=step.sell_amount_raw,
                        sell_ui=step.sell_amount_raw / (10 ** mint_status.decimals),
                        liquidity_cap_raw=mint_state.liquidity_cap.max_sell_raw,
                        cooldown_state="off",
                        action="skipped",
                        reason="24h_cap",
                    )
                    continue

                # Compact status line: include dynamic regimes, step id, liquidity cap and cooldown status.
                now_dt = datetime.now(tz=timezone.utc)
                now = now_dt.strftime("%H:%M:%S")
                price_str = f"{current_price:.2e}"
                cooldown_active = (
                    mint_state.cooldown_until is not None
                    and now_dt < mint_state.cooldown_until
                )
                cooldown_label = "on" if cooldown_active else "off"
                print(
                    f"{now}  {_pair_name(mint_status)}  {price_str}  step_id={step.step_id} "
                    f"(vol={mint_state.volatility.regime} mom={mint_state.momentum.regime} "
                    f"liq_cap={mint_state.liquidity_cap.max_sell_raw or 0} cooldown={cooldown_label})",
                    flush=True,
                )

                if current_price < entry_price * step.multiple:
                    below_target += 1
                    logger.info(
                        "TARGET_NOT_REACHED mint=%s symbol=%s step_id=%s current=%.6e target=%.6e (entry*%.2f)",
                        mint, mint_status.symbol or mint[:8], step.step_id, current_price, entry_price * step.multiple, step.multiple,
                    )
                    _readiness["sell_blocked_reason"] = "below_target"
                    sell_readiness[mint] = _readiness
                    logger.info(
                        "SELL_READINESS mint=%s symbol=%s current=%.6e target=%.6e diff_pct=%s tradable=%s blocked=below_target ready=false",
                        mint, _readiness["symbol"], current_price, target_price, f"{diff_pct:+.1f}%" if diff_pct is not None else "N/A", runtime_tradable_raw,
                    )
                    continue

                # Ready to attempt sell: record and log before quote/execute
                _readiness["sell_ready_now"] = True
                _readiness["sell_blocked_reason"] = ""
                sell_readiness[mint] = _readiness
                logger.info(
                    "SELL_READINESS mint=%s symbol=%s current=%.6e target=%.6e diff_pct=%s tradable=%s blocked=none ready=true",
                    mint, _readiness["symbol"], current_price, target_price, f"{diff_pct:+.1f}%" if diff_pct is not None else "N/A", runtime_tradable_raw,
                )
                logger.info("SELL_READY mint=%s symbol=%s step_id=%s", mint, mint_status.symbol or mint[:8], step.step_id)

                # Quote for full step size (optionally fractured into N smaller sells when liquidity is thin).
                try:
                    sell_ui = step.sell_amount_raw / (10 ** mint_status.decimals)
                    cooldown_label = "on" if (mint_state.cooldown_until and datetime.now(tz=timezone.utc) < mint_state.cooldown_until) else "off"
                    expected_price_sol = 0.0
                    liq_cap_raw = mint_state.liquidity_cap.max_sell_raw
                    trading_bag_raw = int(mint_state.trading_bag_raw)

                    # Micro-sell fracturing: split into N children only when liquidity is below threshold.
                    liquidity_usd = mint_status.market.dexscreener.liquidity_usd or 0
                    fracture_n = 1
                    if config.micro_sell_fracture_n >= 2 and liquidity_usd < config.micro_sell_fracture_when_liquidity_below_usd:
                        fracture_n = min(config.micro_sell_fracture_n, 3)
                    chunks = _fracture_chunks(step.sell_amount_raw, fracture_n)
                    if len(chunks) > 1:
                        probe = get_quote(
                            input_mint=mint,
                            output_mint=WSOL_MINT,
                            amount_raw=chunks[0],
                            slippage_bps=config.slippage_bps,
                            config=config,
                        )
                        if not probe or (int(probe.get("outAmount", 0)) / 1e9) < config.min_trade_sol:
                            chunks = [step.sell_amount_raw]

                    total_sol_out = 0.0
                    total_sold_raw = 0
                    last_sig = None
                    children_done = 0
                    for idx, child_amount in enumerate(chunks):
                        trade_quote = get_quote(
                            input_mint=mint,
                            output_mint=WSOL_MINT,
                            amount_raw=child_amount,
                            slippage_bps=config.slippage_bps,
                            config=config,
                        )
                        quote_ts = time.monotonic()
                        out_amount = int(trade_quote.get("outAmount", 0))
                        sol_out_est = out_amount / 1e9
                        expected_price_sol = (
                            (out_amount / 1e9)
                            / (child_amount / (10 ** mint_status.decimals))
                        )

                        # Runtime sell sanity for this child (or full step).
                        trading_bag_raw = int(mint_state.trading_bag_raw)
                        if child_amount <= 0:
                            logger.warning(
                                "Mint %s step_id=%s skipped: sell_amount_raw <= 0 (invalid step).",
                                _pair_name(mint_status),
                                step.step_id,
                            )
                            logger.info("SELL_SIZE_ZERO mint=%s symbol=%s step_id=%s", mint, mint_status.symbol or mint[:8], step.step_id)
                            _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", "invalid_step")
                            continue
                        if child_amount > trading_bag_raw:
                            logger.warning(
                                "Mint %s step_id=%s skipped: sell_amount_raw (%s) > trading_bag_remaining (%s).",
                                _pair_name(mint_status),
                                step.step_id,
                                child_amount,
                                trading_bag_raw,
                            )
                            logger.info("BALANCE_MISMATCH_BLOCK mint=%s symbol=%s step_id=%s child=%s bag=%s", mint, mint_status.symbol or mint[:8], step.step_id, child_amount, trading_bag_raw)
                            _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", "sanity_bag")
                            continue
                        if liq_cap_raw is not None and child_amount > liq_cap_raw:
                            logger.warning(
                                "Mint %s step_id=%s skipped: sell_amount_raw (%s) > liquidity_cap_raw (%s).",
                                _pair_name(mint_status),
                                step.step_id,
                                child_amount,
                                liq_cap_raw,
                            )
                            logger.info("LIQUIDITY_BLOCK mint=%s symbol=%s step_id=%s child=%s liq_cap=%s", mint, mint_status.symbol or mint[:8], step.step_id, child_amount, liq_cap_raw)
                            liquidity_skips += 1
                            _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", "liquidity_cap")
                            continue

                        if sol_out_est < config.min_trade_sol:
                            min_trade_skips += 1
                            logger.warning(
                                "Mint %s step_id=%s skipped: dust (%.6f SOL < min_trade_sol %.6f).",
                                _pair_name(mint_status),
                                step.step_id,
                                sol_out_est,
                                config.min_trade_sol,
                            )
                            logger.info("SELL_BLOCKED_DUST mint=%s symbol=%s step_id=%s sol_out=%.6f min_trade_sol=%.6f", mint, mint_status.symbol or mint[:8], step.step_id, sol_out_est, config.min_trade_sol)
                            _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", "dust")
                            continue

                        if expected_price_sol < entry_price * 0.9:
                            logger.warning(
                                "Mint %s step_id=%s skipped: quote implied price %.6e < 90%% entry (%.6e); possible route/liquidity error.",
                                _pair_name(mint_status),
                                step.step_id,
                                expected_price_sol,
                                entry_price,
                            )
                            logger.info("SELL_BLOCKED_SLIPPAGE_SANITY mint=%s symbol=%s step_id=%s expected_sol=%.6e entry=%.6e", mint, mint_status.symbol or mint[:8], step.step_id, expected_price_sol, entry_price)
                            liquidity_skips += 1
                            _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", "slippage_sanity")
                            continue

                        impact_pct = None
                        try:
                            impact_pct = float(trade_quote.get("priceImpactPct"))  # type: ignore[arg-type]
                        except Exception:
                            impact_pct = None
                        if impact_pct is not None and impact_pct > 5.0:
                            logger.warning(
                                "Mint %s step_id=%s skipped: price impact %.2f%% exceeds threshold.",
                                _pair_name(mint_status),
                                step.step_id,
                                impact_pct,
                            )
                            logger.info("SELL_BLOCKED_PRICE_IMPACT mint=%s symbol=%s step_id=%s impact_pct=%.2f", mint, mint_status.symbol or mint[:8], step.step_id, impact_pct)
                            liquidity_skips += 1
                            _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", "price_impact")
                            continue

                        if (time.monotonic() - quote_ts) > config.quote_max_age_sec:
                            logger.warning(
                                "Mint %s step_id=%s skipped: quote older than %s s; discard and requote next cycle.",
                                _pair_name(mint_status),
                                step.step_id,
                                config.quote_max_age_sec,
                            )
                            logger.info("SELL_BLOCKED_QUOTE_STALE mint=%s symbol=%s step_id=%s max_age=%.1f", mint, mint_status.symbol or mint[:8], step.step_id, config.quote_max_age_sec)
                            _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", "quote_stale")
                            continue

                        if trading_disabled:
                            logger.info("SELL_BLOCKED_TRADING_DISABLED mint=%s symbol=%s step_id=%s (STOP or RPC pause)", mint, mint_status.symbol or mint[:8], step.step_id)
                            _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", "stop_or_rpc_pause")
                            continue

                        if step_key in mint_state.executed_steps:
                            logger.info("SELL_BLOCKED_STEP_ALREADY_EXECUTED mint=%s symbol=%s step_id=%s", mint, mint_status.symbol or mint[:8], step.step_id)
                            _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", "duplicate_step")
                            continue

                        if monitor_only:
                            logger.info("PROTECTION_ONLY_BLOCK mint=%s symbol=%s step_id=%s reason=monitor_only_or_LIVE_PROTECTION_ONLY", mint, mint_status.symbol or mint[:8], step.step_id)
                            _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", "monitor_only")
                            continue

                        inv_ok, inv_reason = _pre_swap_invariants_ok(
                            mint_state=mint_state,
                            step_key=step_key,
                            step=step,
                            trading_bag_raw=trading_bag_raw,
                            liq_cap_raw=liq_cap_raw,
                            trading_disabled=trading_disabled,
                            quote_ts=quote_ts,
                            amount_raw_override=child_amount,
                            quote_max_age_sec=config.quote_max_age_sec,
                        )
                        if not inv_ok:
                            logger.warning(
                                "Mint %s step_id=%s pre-swap invariant failed: %s; skipping.",
                                _pair_name(mint_status),
                                step.step_id,
                                inv_reason,
                            )
                            _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", inv_reason)
                            continue

                        # Risk engine: block execution when limits violated (CEO directive).
                        liquidity_usd = None
                        if mint_status.market and mint_status.market.dexscreener:
                            liquidity_usd = getattr(mint_status.market.dexscreener, "liquidity_usd", None)
                        wallet_sol = (state.sol.sol if state.sol else 0.0) or 0.0
                        sold_this_hour_raw = 0
                        for (t, raw) in per_mint_sells.get(mint, [])[-100:]:
                            if (now_utc - t).total_seconds() <= 3600:
                                sold_this_hour_raw += raw
                        sold_this_hour_sol = (sold_this_hour_raw / (10 ** mint_status.decimals)) * entry_price if entry_price and mint_status.decimals else 0.0
                        trading_bag_sol_value = (trading_bag_raw / (10 ** mint_status.decimals)) * entry_price if entry_price and mint_status.decimals else 0.0
                        risk_reason = block_execution_reason(
                            liquidity_usd=liquidity_usd,
                            slippage_bps=config.slippage_bps,
                            trade_sol=sol_out_est,
                            wallet_sol=wallet_sol,
                            sold_this_hour_sol=sold_this_hour_sol,
                            trading_bag_sol_value=trading_bag_sol_value or 1e-9,
                        )
                        if risk_reason is not None:
                            logger.warning("%s mint=%s symbol=%s step_id=%s reason=%s", RISK_BLOCK, mint[:12], mint_status.symbol or mint[:8], step.step_id, risk_reason)
                            if event_journal_path:
                                append_event(event_journal_path, RISK_BLOCK, {"mint": mint[:12], "step_id": step.step_id, "reason": risk_reason})
                            _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", f"risk_{risk_reason}")
                            continue

                        # Sell execution safety: re-fetch balance and cap to actual
                        try:
                            refetch = rpc.get_token_account_balance_quick(mint_status.token_account, timeout_s=5.0)
                            actual_now = int(refetch.get("amount", 0)) if refetch and refetch.get("amount") is not None else trading_bag_raw
                        except Exception:
                            actual_now = trading_bag_raw
                        safe_balance = min(actual_now, trading_bag_raw)
                        if child_amount > safe_balance:
                            logger.warning(
                                "SELL_ABORT_BALANCE_MISMATCH mint=%s step_id=%s child_amount=%s safe_balance=%s actual_now=%s",
                                _pair_name(mint_status), step.step_id, child_amount, safe_balance, actual_now,
                            )
                            logger.info("BALANCE_MISMATCH_BLOCK mint=%s symbol=%s step_id=%s child=%s safe=%s", mint, mint_status.symbol or mint[:8], step.step_id, child_amount, safe_balance)
                            if event_journal_path:
                                append_event(event_journal_path, EVENT_SELL_FAILED, {"mint": mint[:12], "step_id": step.step_id, "reason": "balance_mismatch", "child_amount": child_amount, "safe_balance": safe_balance})
                            _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", "balance_mismatch")
                            continue

                        logger.info(
                            "SELL_TRIGGERED mint=%s symbol=%s step_id=%s sell_raw=%s entry=%.6e target_mult=%.4f",
                            mint, mint_status.symbol or mint[:8], step.step_id, child_amount, entry_price, step.multiple,
                        )
                        if event_journal_path:
                            append_event(event_journal_path, EVENT_SELL_SENT, {"mint": mint[:12], "step_id": step.step_id, "amount_raw": child_amount})
                        exec_result = execute_swap(
                            input_mint=mint,
                            output_mint=WSOL_MINT,
                            amount_raw=child_amount,
                            user_pubkey=pubkey,
                            config=config,
                            rpc=rpc,
                            sign_fn=sign_tx,
                        )
                        if not exec_result.success:
                            if event_journal_path:
                                append_event(event_journal_path, EVENT_SELL_FAILED, {"mint": mint[:12], "step_id": step.step_id, "reason": exec_result.error or "execution_engine"})
                            raise RuntimeError(exec_result.error or "execute_swap failed")
                        signature = exec_result.signature or ""
                        confirmed = exec_result.confirmed
                        if not confirmed:
                            if event_journal_path:
                                append_event(event_journal_path, EVENT_SELL_FAILED, {"mint": mint[:12], "step_id": step.step_id, "signature": signature, "reason": "not_confirmed"})
                            raise RuntimeError(f"Transaction {signature} not confirmed in time")

                        run_state["rpc_failures_consecutive"] = 0
                        if event_journal_path:
                            append_event(event_journal_path, EVENT_SELL_CONFIRMED, {"mint": mint[:12], "step_id": step.step_id, "signature": signature})
                            append_event(event_journal_path, EVENT_TP_HIT, {"mint": mint[:12], "step_id": step.step_id, "sold_raw": child_amount})
                        try:
                            from .integration.event_bus import emit as event_bus_emit, SEVERITY_HIGH
                            event_bus_emit(
                                event_journal_path,
                                "SELL_CONFIRMED",
                                "runner",
                                SEVERITY_HIGH,
                                short_message=f"mint={mint[:12]} step={step.step_id}. Lot closed, ladder updated.",
                            )
                        except Exception:
                            pass

                        actual_sol_out = sol_out_est
                        try:
                            tx = rpc.get_transaction(signature)
                            from .tx_infer import _parse_sol_delta_lamports  # type: ignore

                            sol_delta = _parse_sol_delta_lamports(tx, pubkey)
                            if sol_delta is not None:
                                actual_sol_out = abs(sol_delta) / 1e9
                        except Exception:
                            pass
                        total_sol_out += actual_sol_out
                        # Start with requested child amount; may be adjusted below if
                        # post-swap balance reconciliation finds a large mismatch.
                        child_sold_raw = child_amount
                        total_sold_raw += child_sold_raw
                        last_sig = signature
                        children_done += 1

                        balance_before_raw = int(mint_state.trading_bag_raw) + int(mint_state.moonbag_raw)
                        try:
                            value = rpc.get_token_account_balance_quick(mint_status.token_account, timeout_s=5.0)
                            if value and "amount" in value:
                                amount_raw = value.get("amount")
                                if amount_raw is not None:
                                    balance_after_raw = int(amount_raw)
                                    delta = balance_before_raw - balance_after_raw
                                    mismatch_pct = abs(delta - child_amount) / max(child_amount, 1)
                                    if mismatch_pct > BALANCE_RECONCILE_TOLERANCE:
                                        logger.warning(
                                            "Mint %s step_id=%s post-swap balance mismatch: expected_delta=%s observed_delta=%s; resyncing trading_bag from chain.",
                                            _pair_name(mint_status),
                                            step.step_id,
                                            child_amount,
                                            delta,
                                        )
                                        # Use observed wallet delta as best available sold amount.
                                        observed_sold_raw = max(0, delta)
                                        # Adjust cumulative total_sold_raw to reflect observed, not requested.
                                        total_sold_raw -= child_sold_raw
                                        child_sold_raw = observed_sold_raw
                                        total_sold_raw += child_sold_raw
                                        trading_bag_raw, moonbag_raw = compute_trading_bag(
                                            balance_raw=str(balance_after_raw),
                                            trading_bag_pct=config.trading_bag_pct,
                                        )
                                        mint_state.trading_bag_raw = str(trading_bag_raw)
                                        mint_state.moonbag_raw = str(moonbag_raw)
                                        _debit_lots_fifo(mint_state, child_sold_raw)
                                        if getattr(mint_state, "lots", None):
                                            mint_state.trading_bag_raw = str(_trading_bag_from_lots(mint_state))
                        except Exception as exc:
                            logger.debug("Post-swap balance refresh for %s failed: %s", _pair_name(mint_status), exc)

                        if idx < len(chunks) - 1 and len(chunks) > 1:
                            time.sleep(config.micro_sell_fracture_delay_sec)

                    # Mark step executed when all children completed, or total sold meets step (partial fracture).
                    step_complete = (
                        children_done == len(chunks)
                        or total_sold_raw >= step.sell_amount_raw * (1.0 - BALANCE_RECONCILE_TOLERANCE)
                    )
                    if step_complete and total_sold_raw > 0:
                        # Accounting truth must follow actual observed sold amount, not the planned step size.
                        accounted_sold_raw = min(total_sold_raw, step.sell_amount_raw)
                        executed_info = StepExecutionInfo(
                            sig=last_sig or "fractured",
                            time=datetime.now(tz=timezone.utc),
                            sold_raw=str(accounted_sold_raw),
                            sol_out=total_sol_out,
                        )
                        mint_state.executed_steps[step_key] = executed_info
                        _add_sell_accounting(
                            mint_state,
                            bot_delta=accounted_sold_raw,
                            journal_path=event_journal_path,
                            mint=mint,
                            step_key=str(step.step_id),
                            tx_sig=last_sig,
                            sold_raw=accounted_sold_raw,
                            source="bot",
                        )
                        _apply_sell_inventory_effects(
                            mint_state=mint_state,
                            config=config,
                            mint_addr=mint,
                            sold_raw=accounted_sold_raw,
                            journal_path=event_journal_path,
                            tx_signature=last_sig,
                        )
                        per_mint_sells.setdefault(mint, []).append(
                            (executed_info.time, accounted_sold_raw)
                        )
                        per_mint_sells_24h.setdefault(mint, []).append(
                            (executed_info.time, accounted_sold_raw)
                        )

                        _reset_failures(mint_state)
                        base_cooldown = 60.0
                        if mint_state.volatility.regime == "high" and mint_state.momentum.regime == "strong":
                            base_cooldown = 30.0
                        elif mint_state.volatility.regime == "low" or mint_state.momentum.regime == "weak":
                            base_cooldown = 180.0
                        mint_state.last_sell_at = executed_info.time
                        mint_state.cooldown_until = executed_info.time + timedelta(
                            seconds=base_cooldown
                        )
                        sells_executed += 1
                        _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, accounted_sold_raw, sell_ui, liq_cap_raw, cooldown_label, "executed", "ok")
                        logger.info(
                            "SELL_EXECUTED mint=%s symbol=%s step_id=%s sold_raw=%s sol_out=%.6f sig=%s",
                            mint, mint_status.symbol or mint[:8], step.step_id, accounted_sold_raw, total_sol_out, last_sig or "fractured",
                        )
                        logger.info(
                            "Executed step %s for mint %s: sold_raw=%s, sol_out=%.6f, sig=%s fractured=%s",
                            str(step.step_id),
                            _pair_name(mint_status),
                            accounted_sold_raw,
                            total_sol_out,
                            last_sig or "fractured",
                            len(chunks) > 1,
                        )
                        if event_journal_path:
                            proot = event_journal_path.parent
                            sym = mint_status.symbol or mint[:8]
                            dec = getattr(mint_status, "decimals", None) or 6
                            sold_ui = total_sold_raw / (10 ** dec)
                            sold_str = f"{sold_ui / 1e6:.1f}M" if sold_ui >= 1e6 else (f"{sold_ui / 1e3:.1f}K" if sold_ui >= 1e3 else f"{sold_ui:.2f}")
                            body = f"Sell executed — {sym}  Sold: {sold_str} tokens  Received: {total_sol_out:.2f} SOL"
                            if total_sol_out >= 0.1:
                                body += "  (Profit event)"
                            _notify_founder(proot, body, "Mint Ladder", critical=False)
                except (JupiterError, WalletError, Exception) as exc:
                    if event_journal_path:
                        append_event(event_journal_path, EVENT_SELL_FAILED, {"mint": mint[:12], "step_id": step.step_id, "reason": str(exc)[:200]})
                    _handle_rpc_failure(run_state, config, event_journal_path)

                    # Confirm uncertain: treat as temporarily paused; do not retry same step until state is clear.
                    is_confirm_fail = isinstance(exc, RuntimeError) and "not confirmed" in str(exc)
                    if is_confirm_fail:
                        mint_state.failures.paused_until = datetime.now(tz=timezone.utc) + timedelta(
                            minutes=CONFIRM_UNCERTAIN_PAUSE_MINUTES
                        )
                        logger.warning(
                            "Mint %s step_id=%s: confirm uncertain; pausing mint for %s min.",
                            _pair_name(mint_status),
                            step.step_id,
                            CONFIRM_UNCERTAIN_PAUSE_MINUTES,
                        )

                    # Partial-fill / ambiguous: try to determine from chain whether step executed.
                    try:
                        balance_after_raw = None
                        value = rpc.get_token_account_balance_quick(mint_status.token_account, timeout_s=5.0)
                        if value and "amount" in value:
                            balance_after_raw = int(value.get("amount", 0))
                        balance_before_raw = int(mint_state.trading_bag_raw) + int(mint_state.moonbag_raw)
                        if balance_after_raw is not None and balance_before_raw > 0:
                            delta = balance_before_raw - balance_after_raw
                            if delta >= step.sell_amount_raw * (1 - BALANCE_RECONCILE_TOLERANCE):
                                accounted_sold_raw = min(delta, step.sell_amount_raw)
                                executed_info = StepExecutionInfo(
                                    sig="inferred",
                                    time=datetime.now(tz=timezone.utc),
                                    sold_raw=str(accounted_sold_raw),
                                    sol_out=sol_out_est,
                                )
                                mint_state.executed_steps[step_key] = executed_info
                                _add_sell_accounting(
                                    mint_state,
                                    bot_delta=accounted_sold_raw,
                                    journal_path=event_journal_path,
                                    mint=mint,
                                    step_key=str(step.step_id),
                                    sold_raw=accounted_sold_raw,
                                    source="bot",
                                )
                                per_mint_sells.setdefault(mint, []).append((executed_info.time, accounted_sold_raw))
                                per_mint_sells_24h.setdefault(mint, []).append((executed_info.time, accounted_sold_raw))
                                _apply_sell_inventory_effects(
                                    mint_state=mint_state,
                                    config=config,
                                    mint_addr=mint,
                                    sold_raw=accounted_sold_raw,
                                    journal_path=event_journal_path,
                                    tx_signature=None,
                                )
                                mint_state.last_sell_at = executed_info.time
                                mint_state.cooldown_until = executed_info.time + timedelta(seconds=60.0)
                                run_state["rpc_failures_consecutive"] = 0
                                logger.info("Mint %s step_id=%s: inferred executed from balance delta.", _pair_name(mint_status), step.step_id)
                            elif not is_confirm_fail:
                                mint_state.failures.paused_until = datetime.now(tz=timezone.utc) + timedelta(
                                    minutes=CONFIRM_UNCERTAIN_PAUSE_MINUTES
                                )
                    except Exception:
                        if not is_confirm_fail:
                            mint_state.failures.paused_until = datetime.now(tz=timezone.utc) + timedelta(
                                minutes=CONFIRM_UNCERTAIN_PAUSE_MINUTES
                            )

                    _audit_sell(mint, mint_status.symbol, step.step_id, step.multiple, entry_price, expected_price_sol, step.sell_amount_raw, sell_ui, liq_cap_raw, cooldown_label, "skipped", "exception")
                    logger.warning(
                        "Mint %s step_id=%s skipped: failed quote/swap - %s",
                        _pair_name(mint_status),
                        step.step_id,
                        exc,
                    )
                    _update_failures_on_error(mint_state, exc, config, mint=mint)
                    sells_failed += 1

                # Buy-back: when price is below entry by trigger %, spend SOL to buy token (capped). Blocked when STOP/trading_disabled.
                bb = _try_buyback(
                    mint_status=mint_status,
                    mint_state=mint_state,
                    rpc=rpc,
                    config=config,
                    pubkey=pubkey,
                    sign_tx=sign_tx,
                    status_created_at=status_data.created_at,
                    trading_disabled=trading_disabled,
                )
                if bb == "executed":
                    buybacks_executed += 1
                elif bb == "failed":
                    buybacks_failed += 1

            _check_reanchor(state, tradable_mints, cycle_prices, config)

            # Refresh live SOL balance for dashboard/risk: state.sol reflects latest on-chain wallet SOL.
            try:
                lamports = rpc.get_balance(pubkey)
                sol = lamports / 1e9
                if state.sol is None:
                    state.sol = SolBalance(lamports=lamports, sol=sol)
                else:
                    state.sol.lamports = lamports
                    state.sol.sol = sol
            except Exception:
                pass

            # Persist state after each full round.
            save_t0 = time.monotonic()
            save_state_atomic(state_path, state)
            save_ms = (time.monotonic() - save_t0) * 1000.0
            try:
                from .dashboard_server import invalidate_dashboard_cache
                invalidate_dashboard_cache()
            except Exception:
                pass

            # Cycle self-test: state pending count must match what dashboard would serve (single source of truth).
            state_display_pending = _count_display_pending_lots(state)
            try:
                from .dashboard_server import build_dashboard_payload
                payload = build_dashboard_payload(state_path.parent)
                api_pending = int(payload.get("pending_lots_count", 0) or 0)
                if state_display_pending != api_pending:
                    logger.warning(
                        "CYCLE_STATE_MISMATCH cycle=%d state_display_pending=%d api_pending_lots_count=%d (uncorrectable in-process)",
                        cycle, state_display_pending, api_pending,
                    )
                    first = run_state.get("cycle_mismatch_first_detected_at_cycle")
                    if first is None:
                        run_state["cycle_mismatch_first_detected_at_cycle"] = cycle
                    elif (cycle - first) >= 2:
                        if event_journal_path:
                            _notify_founder(
                                event_journal_path.parent,
                                "Cycle mismatch persists — investigation required.",
                                "Mint Ladder",
                                critical=True,
                            )
                        run_state["cycle_mismatch_first_detected_at_cycle"] = None
                else:
                    run_state["cycle_mismatch_first_detected_at_cycle"] = None
            except Exception as e:
                logger.debug("Cycle self-test (dashboard payload) skipped: %s", e)

            cycle_duration_ms = (time.monotonic() - cycle_t0) * 1000.0
            rpc_latency_ms = rpc.measure_latency_ms()

            # Update global totals.
            total_sells_executed += sells_executed
            total_sells_failed += sells_failed
            total_buybacks_executed += buybacks_executed
            total_buybacks_failed += buybacks_failed

            cycle_summary = _build_cycle_summary_fields(
                cycle=cycle,
                cycle_duration_ms=cycle_duration_ms,
                rpc_latency_ms=rpc_latency_ms,
                sells_ok=sells_executed,
                sells_fail=sells_failed,
                buybacks_ok=buybacks_executed,
                buybacks_fail=buybacks_failed,
                paused_mints=paused_mints,
                liquidity_skips=liquidity_skips,
                no_step=no_step,
                price_none=price_none,
                below_target=below_target,
                hourcap_skip=hour_cap_skips,
                min_trade_skip=min_trade_skips,
                display_pending=state_display_pending,
                trading_disabled=trading_disabled,
            )
            logger.info(
                "Cycle %d summary: cycle_duration_ms=%.0f rpc_latency_ms=%.0f sells_ok=%d sells_fail=%d buybacks_ok=%d buybacks_fail=%d paused=%d liquidity_skip=%d no_step=%d price_none=%d below_target=%d hourcap_skip=%d min_trade_skip=%d display_pending=%d trading_disabled=%s save_ms=%.0f",
                cycle_summary["cycle"],
                cycle_summary["cycle_duration_ms"],
                cycle_summary["rpc_latency_ms"],
                cycle_summary["sells_ok"],
                cycle_summary["sells_fail"],
                cycle_summary["buybacks_ok"],
                cycle_summary["buybacks_fail"],
                cycle_summary["paused_mints"],
                cycle_summary["liquidity_skips"],
                cycle_summary["no_step"],
                cycle_summary["price_none"],
                cycle_summary["below_target"],
                cycle_summary["hourcap_skip"],
                cycle_summary["min_trade_skip"],
                cycle_summary["display_pending"],
                str(cycle_summary["trading_disabled"]).lower(),
                save_ms,
            )
            runtime_info = _build_health_runtime_info(
                cycle=cycle,
                rpc_latency_ms=rpc_latency_ms,
                paused_mints=paused_mints,
                clean_start=clean_start,
                backfill_completed=backfill_sentinel.exists(),
                config=config,
                sell_readiness=sell_readiness,
                monitor_only=monitor_only,
                trading_ok=trading_ok,
                last_error=None,
                rpc_failures_consecutive=run_state.get("rpc_failures_consecutive", 0),
                global_trading_paused_until=run_state.get("global_trading_paused_until"),
                cycle_mismatch_first_detected_at_cycle=run_state.get("cycle_mismatch_first_detected_at_cycle"),
                sells_failed=sells_failed,
            )
            write_health_status(state_path.parent, state, runtime_info)
            if single_cycle:
                break
            if max_cycles is not None and cycle >= max_cycles:
                logger.info("Reached max_cycles=%d; exiting.", max_cycles)
                try:
                    from .notifier import notify_phase_done
                    notify_phase_done(
                        "runtime_validation",
                        f"{max_cycles} cycles completed. Check runtime_validation.log / run.log.",
                        "Review docs/RUNTIME_VALIDATION_REPORT.md",
                    )
                except Exception:
                    pass
                break
            time.sleep(config.check_interval_sec)
    finally:
        logger.info(
            "Run totals: sells_ok=%d sells_fail=%d buybacks_ok=%d buybacks_fail=%d",
            total_sells_executed,
            total_sells_failed,
            total_buybacks_executed,
            total_buybacks_failed,
        )
        rpc.close()


def run_one_wallet_lane(
    wallet_id: str,
    lane_id: str,
    state_path: Path,
    status_path: Path,
    config: Config,
    run_state: Dict[str, Any],
    dry_run: bool,
) -> str:
    """
    Run one cycle-slice for a single (wallet_id, lane_id): load state/status,
    apply STOP and global RPC pause (return "guard_blocked" if active),
    then run one cycle with T7 guards. When dry_run is True, no get_swap_tx/sign/send.
    Returns: "guard_blocked" | "dry_run" | "stubbed" | "ok".
    """
    # Load state and status (contract: load from paths; run_bot will reload for the cycle).
    if not state_path.exists() or not status_path.exists():
        logger.warning(
            "run_one_wallet_lane wallet_id=%s lane_id=%s: state or status path missing; skipping.",
            wallet_id,
            lane_id,
        )
        return "guard_blocked"
    try:
        state = load_state(state_path, status_path)
        status_data = StatusFile.model_validate_json(status_path.read_text())
    except Exception as exc:
        logger.warning(
            "run_one_wallet_lane wallet_id=%s lane_id=%s: load failed %s; skipping.",
            wallet_id,
            lane_id,
            exc,
        )
        return "guard_blocked"
    # Backfill reanchor fields for legacy state.
    for ms in state.mints.values():
        if getattr(ms, "original_entry_price_sol_per_token", None) is None:
            ms.original_entry_price_sol_per_token = ms.entry_price_sol_per_token
        if getattr(ms, "working_entry_price_sol_per_token", None) is None:
            ms.working_entry_price_sol_per_token = ms.entry_price_sol_per_token
    # Pre-run guards: STOP file and global RPC pause (same as engine pre_run_risk_check).
    if _stop_file_present(state_path):
        return "guard_blocked"
    now_utc = datetime.now(tz=timezone.utc)
    global_pause_until = run_state.get("global_trading_paused_until")
    if global_pause_until is not None and now_utc < global_pause_until:
        return "guard_blocked"
    # One cycle with monitor_only=dry_run; no live execution when dry_run.
    run_bot(
        status_path=status_path,
        state_path=state_path,
        config=config,
        monitor_only=dry_run,
        single_cycle=True,
        wallet_id=wallet_id,
    )
    return "dry_run" if dry_run else "ok"

