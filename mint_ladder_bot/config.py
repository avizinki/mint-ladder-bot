import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .runtime_paths import (
    get_events_path,
    get_state_path,
    get_status_path,
)


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _jupiter_urls() -> tuple[str, str]:
    base = os.getenv("JUPITER_BASE_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/swap/v1/quote", f"{base}/swap/v1/swap"
    return (
        os.getenv("JUPITER_QUOTE_URL", "https://api.jup.ag/swap/v1/quote"),
        os.getenv("JUPITER_SWAP_URL", "https://api.jup.ag/swap/v1/swap"),
    )


@dataclass
class Config:
    # RPC / Jupiter
    rpc_endpoint: str = os.getenv("RPC_ENDPOINT", "https://mainnet.helius-rpc.com/?api-key=demo")
    rpc_timeout_s: float = _env_float("RPC_TIMEOUT_SEC", 20.0)
    jupiter_quote_url: str = ""
    jupiter_swap_url: str = ""
    jupiter_api_key: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.jupiter_quote_url or not self.jupiter_swap_url:
            self.jupiter_quote_url, self.jupiter_swap_url = _jupiter_urls()
        # Read at runtime so .env-loaded vars are visible (main loads .env before Config())
        if self.jupiter_api_key is None:
            self.jupiter_api_key = os.getenv("JUPITER_API_KEY") or None
        # Buy-back enabled flag from env (default off)
        buyback = os.getenv("BUYBACK_ENABLED", "").strip().lower()
        self.buyback_enabled = buyback in ("1", "true", "yes")
        # Live validation: protection-only mode (no autonomous buying)
        live_prot = os.getenv("LIVE_PROTECTION_ONLY", "").strip().lower()
        self.live_protection_only = live_prot in ("1", "true", "yes")
        # Global safety switch (CEO directive): trading allowed only when TRADING_ENABLED=true.
        trading_en = os.getenv("TRADING_ENABLED", "").strip().lower()
        self.trading_enabled = trading_en == "true"
        # Monitor mode default (CEO directive §11): live trading requires LIVE_TRADING=true.
        live_tr = os.getenv("LIVE_TRADING", "false").strip().lower()
        self.live_trading = live_tr == "true"
        # Kill switch: env overrides (STOP file still checked)
        trading_dis = os.getenv("TRADING_DISABLED", "").strip().lower()
        self.trading_disabled_env = trading_dis in ("1", "true", "yes")
        break_even = os.getenv("BREAK_EVEN_ENABLED", "").strip().lower()
        self.break_even_enabled = break_even in ("1", "true", "yes")
        # Paths derived from state_path (safety and event journal next to state)
        sp = getattr(self, "state_path", None) or Path("state.json")
        self.safety_state_path = sp.parent / "safety_state.json"
        self.event_journal_path = sp.parent / "events.jsonl"
        # Sniper
        sniper = os.getenv("SNIPER_ENABLED", "").strip().lower()
        self.sniper_enabled = sniper in ("1", "true", "yes")
        self.pumpfun_new_tokens_url = (os.getenv("PUMPFUN_NEW_TOKENS_URL") or "").strip() or None
        # Transfer provenance: trusted source wallets (explicit allow-list; empty = all untrusted in analysis)
        trusted_env = (os.getenv("TRUSTED_SOURCE_WALLETS") or "").strip()
        self.trusted_source_wallets = [w.strip() for w in trusted_env.split(",") if w.strip()] if trusted_env else []
        # Manual override inventory: explicit per-mint allow-list; empty = none.
        override_env = (os.getenv("MANUAL_OVERRIDE_ALLOWED_MINTS") or "").strip()
        self.manual_override_allowed_mints = [m.strip() for m in override_env.split(",") if m.strip()] if override_env else []
        # Manual override reconciliation bypass: global enable + per-mint allow-list (off by default).
        bypass_env = (os.getenv("MANUAL_OVERRIDE_BYPASS_ALLOWED_MINTS") or "").strip()
        self.manual_override_bypass_allowed_mints = [m.strip() for m in bypass_env.split(",") if m.strip()] if bypass_env else []
        # Discovery source allowlist (empty = all registered sources are allowed)
        disc_allow = (os.getenv("DISCOVERY_SOURCE_ALLOWLIST") or "").strip()
        self.discovery_source_allowlist = [s.strip() for s in disc_allow.split(",") if s.strip()] if disc_allow else []

    # Trading
    trading_bag_pct: float = _env_float("TRADING_BAG_PCT", 0.20)
    slippage_bps: int = _env_int("SLIPPAGE_BPS", 75)
    min_trade_sol: float = _env_float("MIN_TRADE_SOL", 0.01)
    check_interval_sec: float = _env_float("CHECK_INTERVAL_SEC", 15.0)
    max_retries: int = _env_int("MAX_RETRIES", 5)

    # T41: wallet buy detection — ignore balance increases below this (raw units) to avoid dust/airdrop
    min_buy_detection_raw: int = _env_int("MIN_BUY_DETECTION_RAW", 10_000)

    # Tx-first: when balance increase has no matching tx, allow creating one fallback lot with source=unknown (default off).
    allow_snapshot_fallback: bool = os.getenv("ALLOW_SNAPSHOT_FALLBACK", "").strip().lower() in ("1", "true", "yes")

    # Transfer provenance (Step 2: read-only analysis). Design: docs/trading/launch-time-reconstruction-transfer-provenance-design.md
    # Comma-separated wallet pubkeys; transfers from these are classified as trusted-transfer-candidate (analysis only).
    trusted_source_wallets: List[str] = field(default_factory=list)  # set in __post_init__ from TRUSTED_SOURCE_WALLETS
    reconstruction_max_signatures_per_wallet: int = _env_int("RECONSTRUCTION_MAX_SIGNATURES_PER_WALLET", 500)

    # Entry inference
    # Maximum number of signatures to inspect for entry-price inference.
    # Set to 0 to disable inference entirely.
    entry_infer_signature_limit: int = _env_int("ENTRY_INFER_SIGNATURE_LIMIT", 60)

    # One-off backfill: try to enrich existing snapshot lots with tx_signature and real buy price from chain.
    backfill_lot_tx_once: bool = os.getenv("BACKFILL_LOT_TX_ONCE", "").strip().lower() in ("1", "true", "yes")

    # Bootstrap buy for unknown-entry mints (one tiny SOL→token buy to establish entry from chain).
    bootstrap_buy_sol: float = _env_float("BOOTSTRAP_BUY_SOL", 0.01)

    # Re-anchoring (adaptive entry raise after sustained price rise)
    reanchor_cycles_required: int = _env_int("REANCHOR_CYCLES_REQUIRED", 3)
    reanchor_cooldown_hours: float = _env_float("REANCHOR_COOLDOWN_HOURS", 24.0)
    reanchor_max_per_24h: int = _env_int("REANCHOR_MAX_PER_24H", 2)

    # Safety
    price_stale_threshold_sec: float = _env_float(
        "PRICE_STALE_THRESHOLD_SEC", 20.0
    )
    liquidity_warn_threshold_usd: float = _env_float(
        "LIQUIDITY_WARN_THRESHOLD_USD", 5000.0
    )
    max_sell_bag_fraction_per_hour: float = _env_float(
        "MAX_SELL_BAG_FRACTION_PER_HOUR", 1.0
    )
    max_sell_bag_fraction_per_24h: float = _env_float(
        "MAX_SELL_BAG_FRACTION_PER_24H", 2.0
    )
    fail_pause_minutes: float = _env_float("FAIL_PAUSE_MINUTES", 10.0)
    max_consecutive_failures: int = _env_int("MAX_CONSECUTIVE_FAILURES", 3)
    quote_max_age_sec: float = _env_float("QUOTE_MAX_AGE_SEC", 5.0)
    rpc_failures_threshold: int = _env_int("RPC_FAILURES_THRESHOLD", 5)
    rpc_cooldown_sec: float = _env_float("RPC_COOLDOWN_SEC", 120.0)

    # Pump detection (short-term positive return; used for mode/guards later)
    pump_threshold_1m_pct: float = _env_float("PUMP_THRESHOLD_1M_PCT", 10.0)
    pump_threshold_5m_pct: float = _env_float("PUMP_THRESHOLD_5M_PCT", 20.0)

    # Micro-sell fracturing: split one step into N smaller sells when liquidity is thin (execution shape only).
    micro_sell_fracture_n: int = _env_int("MICRO_SELL_FRACTURE_N", 1)  # 1 = off, 2 or 3 = max children
    micro_sell_fracture_when_liquidity_below_usd: float = _env_float(
        "MICRO_SELL_FRACTURE_WHEN_LIQUIDITY_BELOW_USD", 100_000.0
    )
    micro_sell_fracture_delay_sec: float = _env_float("MICRO_SELL_FRACTURE_DELAY_SEC", 2.0)

    # Rug / liquidity-collapse guard: pause mint when liquidity drops sharply or goes null
    liquidity_collapse_drop_pct: float = _env_float("LIQUIDITY_COLLAPSE_DROP_PCT", 0.50)
    liquidity_collapse_pause_minutes: float = _env_float("LIQUIDITY_COLLAPSE_PAUSE_MINUTES", 60.0)
    liquidity_collapse_min_reference_usd: float = _env_float(
        "LIQUIDITY_COLLAPSE_MIN_REFERENCE_USD", 1000.0
    )

    # Live validation: protection-only, stop loss, break-even, dust filter
    live_protection_only: bool = False  # set in __post_init__ from LIVE_PROTECTION_ONLY
    trading_enabled: bool = False  # set in __post_init__ from TRADING_ENABLED (true = allow execution; default false = safe mode)
    live_trading: bool = False  # set in __post_init__ from LIVE_TRADING (default false = monitor only)
    trading_disabled_env: bool = False  # set in __post_init__ from TRADING_DISABLED (kill switch)
    stop_loss_pct: float = _env_float("STOP_LOSS_PCT", 0.15)  # 15% below entry → trigger stop-loss sell
    break_even_enabled: bool = False  # set in __post_init__ from BREAK_EVEN_ENABLED
    break_even_trigger_pct: float = _env_float("BREAK_EVEN_TRIGGER_PCT", 0.05)  # 5% above entry → lock to break-even
    min_liquidity_usd_for_track: float = _env_float("MIN_LIQUIDITY_USD_FOR_TRACK", 1000.0)  # for trading guard
    discover_min_liquidity_usd: float = _env_float("DISCOVER_MIN_LIQUIDITY_USD", 0.0)  # 0 = add any new mint; trading still guarded by min_liquidity_usd_for_track
    circuit_breaker_consecutive_failures: int = _env_int("CIRCUIT_BREAKER_FAILURES", 5)  # pause all after X consecutive

    # Buy-back (optional; default off)
    buyback_enabled: bool = False  # set via BUYBACK_ENABLED=true in __post_init__
    buyback_trigger_pct: float = _env_float("BUYBACK_TRIGGER_PCT", 0.10)
    buyback_max_sol_per_trade: float = _env_float("BUYBACK_MAX_SOL_PER_TRADE", 0.01)
    buyback_max_sol_per_mint: float = _env_float("BUYBACK_MAX_SOL_PER_MINT", 0.1)
    buyback_min_sol: float = _env_float("BUYBACK_MIN_SOL", 0.001)
    buyback_sol_reserve: float = _env_float("BUYBACK_SOL_RESERVE", 0.05)
    buyback_cooldown_sec: float = _env_float("BUYBACK_COOLDOWN_SEC", 3600.0)

    # Live validation hardening: duplicate detection, quarantine, lot mode, event journal
    lot_mode: str = os.getenv("LOT_MODE", "new_lot_per_buy").strip().lower()  # new_lot_per_buy | aggregate_position
    quarantine_duration_sec: float = _env_float("QUARANTINE_DURATION_SEC", 60.0)
    max_processed_signatures: int = _env_int("MAX_PROCESSED_SIGNATURES", 5000)
    max_processed_fingerprints: int = _env_int("MAX_PROCESSED_FINGERPRINTS", 5000)

    # Sniper (entry engine) — legacy fields (kept for backward compatibility; Phase 1 uses new keys below)
    sniper_enabled: bool = False  # set in __post_init__ from SNIPER_ENABLED (legacy gating)
    sniper_buy_sol: float = _env_float("SNIPER_BUY_SOL", 0.02)
    sniper_max_concurrent_lots: int = _env_int("SNIPER_MAX_CONCURRENT_LOTS", 5)
    sniper_cooldown_seconds: float = _env_float("SNIPER_COOLDOWN_SECONDS", 30.0)
    sniper_skip_existing_mints: bool = os.getenv("SNIPER_SKIP_EXISTING_MINTS", "true").strip().lower() in ("1", "true", "yes")
    sniper_min_sol_reserve: float = _env_float("SNIPER_MIN_SOL_RESERVE", 0.1)
    pumpfun_new_tokens_url: Optional[str] = None  # set in __post_init__ from PUMPFUN_NEW_TOKENS_URL
    pumpfun_poll_interval_seconds: float = _env_float("PUMPFUN_POLL_INTERVAL_SECONDS", 30.0)

    # Sniper Phase 1 (manual-seed sniper) — config contract from SNIPER_IMPLEMENTATION_SPEC.md
    sniper_mode: str = os.getenv("SNIPER_MODE", "disabled").strip().lower()  # disabled | paper | live
    sniper_discovery_enabled: bool = os.getenv("SNIPER_DISCOVERY_ENABLED", "").strip().lower() in ("1", "true", "yes")
    sniper_max_candidates_per_cycle: int = _env_int("SNIPER_MAX_CANDIDATES_PER_CYCLE", 3)
    sniper_max_manual_queue_size: int = _env_int("SNIPER_MAX_MANUAL_QUEUE_SIZE", 100)
    sniper_default_buy_sol: float = _env_float("SNIPER_DEFAULT_BUY_SOL", 0.1)
    sniper_min_buy_sol: float = _env_float("SNIPER_MIN_BUY_SOL", 0.02)
    sniper_max_buy_sol: float = _env_float("SNIPER_MAX_BUY_SOL", 0.5)
    sniper_wallet_sol_reserve: float = _env_float("SNIPER_WALLET_SOL_RESERVE", 1.0)
    sniper_max_total_open_risk_sol: float = _env_float("SNIPER_MAX_TOTAL_OPEN_RISK_SOL", 5.0)
    sniper_max_concurrent_positions: int = _env_int("SNIPER_MAX_CONCURRENT_SNIPER_POSITIONS", 5)
    sniper_max_buys_per_hour: int = _env_int("SNIPER_MAX_BUYS_PER_HOUR", 3)
    sniper_max_buys_per_day: int = _env_int("SNIPER_MAX_BUYS_PER_DAY", 10)
    sniper_reentry_cooldown_seconds: int = _env_int("SNIPER_REENTRY_COOLDOWN_SECONDS", 3600)
    sniper_reattempt_cooldown_seconds: int = _env_int("SNIPER_REATTEMPT_COOLDOWN_SECONDS", 900)
    sniper_attempt_uncertain_timeout_seconds: int = _env_int("SNIPER_ATTEMPT_UNCERTAIN_TIMEOUT_SECONDS", 600)
    sniper_min_liquidity_sol_equiv: float = _env_float("SNIPER_MIN_LIQUIDITY_SOL_EQUIV", 50.0)
    sniper_max_slippage_bps: int = _env_int("SNIPER_MAX_SLIPPAGE_BPS", 300)
    sniper_min_score: float = _env_float("SNIPER_MIN_SCORE", 0.6)
    sniper_max_attempt_history: int = _env_int("SNIPER_MAX_ATTEMPT_HISTORY", 100)
    sniper_max_decision_history: int = _env_int("SNIPER_MAX_DECISION_HISTORY", 200)
    sniper_max_processed_signatures: int = _env_int("SNIPER_MAX_PROCESSED_SIGNATURES", 500)
    sniper_cooldown_retention_seconds: int = _env_int("SNIPER_COOLDOWN_RETENTION_SECONDS", 604800)
    sniper_allow_tier2_success: bool = os.getenv("SNIPER_ALLOW_TIER2_SUCCESS", "false").strip().lower() in ("1", "true", "yes")

    # Discovery phase — all gates default to safe/disabled so live runner is unaffected.
    # DISCOVERY_ENABLED=true enables the discovery pipeline. Default false = no-op.
    discovery_enabled: bool = os.getenv("DISCOVERY_ENABLED", "").strip().lower() in ("1", "true", "yes")
    # Comma-sep source ids to allow. Empty = all registered sources allowed.
    discovery_source_allowlist: List[str] = field(default_factory=list)  # set in __post_init__
    # Max new candidates to process per cycle. Prevents flood into execution queue.
    discovery_max_candidates_per_cycle: int = _env_int("DISCOVERY_MAX_CANDIDATES_PER_CYCLE", 5)
    # DISCOVERY_REVIEW_ONLY=true (default): candidates are recorded but never enqueued for execution.
    # Must explicitly set false to allow auto-enqueue (still gated by sniper_mode).
    discovery_review_only: bool = os.getenv("DISCOVERY_REVIEW_ONLY", "true").strip().lower() not in ("0", "false", "no")
    # Optional path to watchlist YAML/JSON file (list of mints or {mint, symbol, note}).
    discovery_watchlist_path: Optional[str] = os.getenv("DISCOVERY_WATCHLIST_PATH") or None
    # Bounded history sizes for dashboard.
    discovery_max_history: int = _env_int("DISCOVERY_MAX_HISTORY", 200)
    discovery_max_rejected: int = _env_int("DISCOVERY_MAX_REJECTED", 200)

    # Files
    # Defaults point at centralized runtime paths; callers may override explicitly.
    status_path: Path = get_status_path()
    state_path: Path = get_state_path()
    safety_state_path: Optional[Path] = None  # set in __post_init__ to state_path.parent / "safety_state.json"
    event_journal_path: Optional[Path] = None  # set in __post_init__ to state_path.parent / "events.jsonl"
    # Manual override inventory (legacy / gifted / non-tx-proven holdings)
    # Disabled by default; explicit per-mint allow-list controls which mints may use it.
    enable_manual_override_inventory: bool = os.getenv("ENABLE_MANUAL_OVERRIDE_INVENTORY", "").strip().lower() in ("1", "true", "yes")
    manual_override_allowed_mints: List[str] = field(default_factory=list)
    manual_override_require_reason: bool = os.getenv("MANUAL_OVERRIDE_REQUIRE_REASON", "true").strip().lower() in ("1", "true", "yes")
    # Manual override reconciliation bypass (per-mint, off by default)
    manual_override_bypass_enabled: bool = os.getenv("MANUAL_OVERRIDE_BYPASS_ENABLED", "").strip().lower() in ("1", "true", "yes")
    manual_override_bypass_allowed_mints: List[str] = field(default_factory=list)
    manual_override_bypass_require_operator_approval: bool = os.getenv("MANUAL_OVERRIDE_BYPASS_REQUIRE_OPERATOR_APPROVAL", "true").strip().lower() in ("1", "true", "yes")
    manual_override_bypass_min_override_raw: int = _env_int("MANUAL_OVERRIDE_BYPASS_MIN_OVERRIDE_RAW", 0)

