from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional
import uuid

from pydantic import BaseModel, Field


class RpcInfo(BaseModel):
    endpoint: str
    latency_ms: Optional[float] = None


class SolBalance(BaseModel):
    lamports: int
    sol: float


class EntryInfo(BaseModel):
    mode: Literal["auto", "manual"] = "auto"
    entry_price_sol_per_token: float = 0.0
    entry_source: Literal["user", "inferred_from_tx", "bootstrap_buy", "market_bootstrap", "unknown"] = "unknown"
    entry_tx_signature: Optional[str] = None


class BootstrapInfo(BaseModel):
    """Tracks one-time bootstrap buy (SOL→token) for unknown-entry mints."""

    bootstrap_pending: bool = True
    bootstrap_started_at: Optional[datetime] = None
    bootstrap_completed_at: Optional[datetime] = None
    bootstrap_sig: Optional[str] = None
    bootstrap_sol_spent: Optional[float] = None
    bootstrap_tokens_received: Optional[str] = None  # raw amount as string


class DexscreenerTxns24h(BaseModel):
    buys: Optional[int] = None
    sells: Optional[int] = None


class DexscreenerMarketInfo(BaseModel):
    pair_address: Optional[str] = None
    dex_id: Optional[str] = None
    liquidity_usd: Optional[float] = None
    price_usd: Optional[float] = None
    price_native: Optional[float] = None
    volume24h_usd: Optional[float] = None
    txns24h: DexscreenerTxns24h = Field(
        default_factory=DexscreenerTxns24h
    )


class MarketInfo(BaseModel):
    dexscreener: DexscreenerMarketInfo = Field(
        default_factory=DexscreenerMarketInfo
    )


class MintStatus(BaseModel):
    mint: str
    token_account: str
    decimals: int
    balance_ui: float
    balance_raw: str
    symbol: Optional[str] = None
    name: Optional[str] = None
    entry: EntryInfo = Field(default_factory=EntryInfo)
    market: MarketInfo = Field(default_factory=MarketInfo)


class StatusFile(BaseModel):
    version: int = 1
    created_at: datetime
    wallet: str
    rpc: RpcInfo
    sol: SolBalance
    mints: List[MintStatus]


class StepExecutionInfo(BaseModel):
    sig: str
    time: datetime
    sold_raw: str
    sol_out: float


class FailureInfo(BaseModel):
    count: int = 0
    last_error: Optional[str] = None
    paused_until: Optional[datetime] = None


class BuybackInfo(BaseModel):
    """Tracks SOL spent on buy-backs per mint for caps and cooldown."""

    total_sol_spent: float = 0.0
    last_buy_at: Optional[datetime] = None
    last_sig: Optional[str] = None


class PriceSample(BaseModel):
    """Single price observation for dynamic ladder analytics."""

    t: datetime
    price: float


class VolatilityInfo(BaseModel):
    """Cached volatility metrics and regime classification for a mint."""

    regime: Literal["low", "medium", "high"] = "medium"
    realized_1m: Optional[float] = None
    realized_5m: Optional[float] = None
    realized_15m: Optional[float] = None


class MomentumInfo(BaseModel):
    """Cached momentum / flow metrics for a mint."""

    regime: Literal["weak", "neutral", "strong"] = "neutral"
    score: float = 0.0


class PumpInfo(BaseModel):
    """Short-term pump detection: positive return over 1m/5m above threshold."""

    detected: bool = False
    return_1m: Optional[float] = None  # fraction, e.g. 0.15 = +15%
    return_5m: Optional[float] = None


class LiquidityCapInfo(BaseModel):
    """Liquidity / price-impact based cap on per-step size (raw units)."""

    max_sell_raw: Optional[int] = None


LotStatus = Literal["active", "fully_sold", "merged", "duplicate_explained"]
CostBasisConfidence = Literal["known", "inferred", "unknown"]
# Entry estimate confidence: exact=from swap tx, inferred=tx+quote, snapshot=initial_migration only, pending_price_resolution=tx lookup in progress, unknown=no tx
EntryConfidence = Literal["exact", "inferred", "snapshot", "bootstrap", "pending_price_resolution", "unknown"]
# Lot source: tx_exact/tx_parsed = from chain tx only; bootstrap_snapshot = migration/snapshot (excluded from trading bag unless confirmed); buyback = bot buyback
LotSource = Literal[
    "tx_exact", "tx_parsed", "quote_at_detection", "snapshot", "buyback",
    "inferred", "unknown", "wallet_buy_detected", "bootstrap_buy", "initial_migration", "bootstrap_snapshot",
    "trusted_transfer_derived",  # Step 3 scratch: proposed lot from trusted source wallet (scratch-only)
]


class ManualOverrideRecord(BaseModel):
    """
    Explicit, operator-approved manual override inventory for a mint.

    Represents legacy / gifted / non-tx-proven holdings that the operator
    chooses to expose to the system. Never created automatically; separate
    from tx-proven lots.
    """

    mint: str
    symbol: Optional[str] = None
    amount_raw: int
    manual_entry_price_sol_per_token: Optional[float] = None
    reason: str
    provenance_note: str
    operator_approved: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    approval_id: Optional[str] = None
    created_by: Optional[str] = None


class LotInfo(BaseModel):
    """One buy lot for dynamic sell allocation (T41)."""

    lot_id: str = ""
    mint: str = ""
    wallet_id: Optional[str] = None
    source: str = "wallet_buy_detected"  # LotSource: tx_exact | tx_parsed | snapshot | buyback | ...
    detected_at: Optional[datetime] = None
    token_amount: str = "0"  # raw
    remaining_amount: str = "0"  # raw
    entry_price_sol_per_token: Optional[float] = None
    cost_basis_confidence: CostBasisConfidence = "unknown"
    entry_confidence: EntryConfidence = "snapshot"  # exact | inferred | snapshot
    status: LotStatus = "active"
    tx_signature: Optional[str] = None  # chain tx that caused this buy (when source is tx_exact/tx_parsed)
    # Swap classification (tx_exact/tx_parsed): sol_to_token | token_to_token | token_to_sol | multi_hop
    swap_type: Optional[str] = None
    input_asset_mint: Optional[str] = None
    input_asset_symbol: Optional[str] = None
    input_amount: Optional[str] = None  # raw amount as string
    acquired_via_swap: bool = False  # True when source is token_to_token or multi_hop
    valuation_method: Optional[str] = None  # sol_spent | source_lot_cost | wsol_equivalent | unknown
    output_asset_symbol: Optional[str] = None  # symbol for mint (output) when known
    entry_price_usd_per_token: Optional[float] = None
    # Trust model: explicit category for dashboard (tx_swap_exact | tx_parsed | bootstrap_snapshot | transfer_received_unknown | buyback)
    source_type: Optional[str] = None
    program_or_venue: Optional[str] = None  # e.g. "jupiter", "raydium" when known

    @classmethod
    def create(
        cls,
        mint: str,
        token_amount_raw: int,
        entry_price: Optional[float] = None,
        confidence: CostBasisConfidence = "unknown",
        source: str = "wallet_buy_detected",
        wallet_id: Optional[str] = None,
        entry_confidence: EntryConfidence = "snapshot",
        tx_signature: Optional[str] = None,
        detected_at: Optional[datetime] = None,
        swap_type: Optional[str] = None,
        input_asset_mint: Optional[str] = None,
        input_asset_symbol: Optional[str] = None,
        input_amount_raw: Optional[int] = None,
        output_asset_symbol: Optional[str] = None,
        entry_price_usd_per_token: Optional[float] = None,
        source_type: Optional[str] = None,
        program_or_venue: Optional[str] = None,
        acquired_via_swap: bool = False,
        valuation_method: Optional[str] = None,
    ) -> "LotInfo":
        now = datetime.now(tz=timezone.utc)
        # Normalize source_type from source for trust model (tx_swap_exact, bootstrap_snapshot, etc.)
        st = source_type
        if st is None and source in ("tx_exact", "tx_parsed"):
            st = "tx_swap_exact" if source == "tx_exact" else "tx_parsed"
        elif st is None and source == "bootstrap_snapshot":
            st = "bootstrap_snapshot"
        elif st is None and source in ("initial_migration", "snapshot"):
            st = "bootstrap_snapshot"
        return cls(
            lot_id=str(uuid.uuid4()),
            mint=mint,
            wallet_id=wallet_id,
            source=source,
            detected_at=detected_at if detected_at is not None else now,
            token_amount=str(token_amount_raw),
            remaining_amount=str(token_amount_raw),
            entry_price_sol_per_token=entry_price,
            cost_basis_confidence=confidence,
            entry_confidence=entry_confidence,
            status="active",
            tx_signature=tx_signature,
            swap_type=swap_type,
            input_asset_mint=input_asset_mint,
            input_asset_symbol=input_asset_symbol,
            input_amount=str(input_amount_raw) if input_amount_raw is not None else None,
            output_asset_symbol=output_asset_symbol,
            entry_price_usd_per_token=entry_price_usd_per_token,
            source_type=st,
            program_or_venue=program_or_venue,
            acquired_via_swap=acquired_via_swap,
            valuation_method=valuation_method,
        )


class RuntimeMintState(BaseModel):
    entry_price_sol_per_token: float
    entry_source: Optional[str] = None  # "user", "inferred_from_tx", "bootstrap_buy", "market_bootstrap", "unknown"
    bootstrap_from_market: bool = False
    bootstrap_timestamp: Optional[datetime] = None
    # Re-anchoring: original never changes; ladder uses working_entry_price_sol_per_token.
    original_entry_price_sol_per_token: Optional[float] = None
    working_entry_price_sol_per_token: Optional[float] = None
    last_reanchor_at: Optional[datetime] = None
    reanchor_count: int = 0
    reanchor_at_times: List[datetime] = Field(default_factory=list)  # rolling 24h for rate limit
    cycles_price_above_working: int = 0
    trading_bag_raw: str
    moonbag_raw: str
    executed_steps: Dict[str, StepExecutionInfo] = Field(default_factory=dict)
    failures: FailureInfo = Field(default_factory=FailureInfo)
    buybacks: BuybackInfo = Field(default_factory=BuybackInfo)
    bootstrap: BootstrapInfo = Field(default_factory=BootstrapInfo)
    # Dynamic ladder state
    price_history: List[PriceSample] = Field(default_factory=list)
    volatility: VolatilityInfo = Field(default_factory=VolatilityInfo)
    momentum: MomentumInfo = Field(default_factory=MomentumInfo)
    pump: PumpInfo = Field(default_factory=PumpInfo)
    liquidity_cap: LiquidityCapInfo = Field(default_factory=LiquidityCapInfo)
    last_sell_at: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None
    # Rug / liquidity-collapse guard: reference level and check time
    reference_liquidity_usd: Optional[float] = None
    last_liquidity_check_at: Optional[datetime] = None
    # T41: wallet buy detection and dynamic lots
    lots: List[LotInfo] = Field(default_factory=list)
    last_known_balance_raw: Optional[str] = None
    # CEO: separate bot vs external sells (invariant: sold_bot_raw + sold_external_raw == sum(executed_steps[].sold_raw))
    sold_bot_raw: Optional[str] = None  # token amount sold by ladder engine (SELL_CONFIRMED / LADDER_LEVEL_FILLED)
    sold_external_raw: Optional[str] = None  # token amount sold by wallet outside bot, ingested via EXTERNAL_SELL_INGESTED
    # Live validation: break-even lock (after price >= entry * (1 + trigger), lock to entry)
    break_even_done: bool = False
    # Quarantine: new tokens enter quarantine before ACTIVE_PROTECTION
    protection_state: Literal["quarantine", "active"] = "active"
    quarantine_until: Optional[datetime] = None
    # CEO directive: runtime truth for entry and tradability (set each cycle)
    tradable: Optional[bool] = None
    tradable_reason: Optional[str] = None
    entry_validation_status: Optional[Literal["valid", "invalid", "unknown"]] = None
    entry_resolution_source: Optional[str] = None  # exact_tx | inferred_from_tx | market_bootstrap | user | unknown
    # Reconciliation guard: track persistent wallet vs lots mismatches for per-mint pause.
    reconcile_mismatch_consecutive: int = 0
    reconcile_mismatch_last_seen_at: Optional[datetime] = None
    # Same-mint external excess quarantine: keep tx-proven core tradable, quarantine only the excess.
    external_excess_raw: Optional[str] = None  # wallet_balance_raw - tx_proven_raw when wallet >= tx_proven_raw > 0
    reconciliation_mode: Optional[str] = None  # "external_excess" | "underwater" | None
    # Manual override inventory: explicit, operator-approved non-tx-proven holdings.
    # Separate from tx-derived lots; may become tradable only when config + allow-list permit.
    manual_override_inventory: List[ManualOverrideRecord] = Field(default_factory=list)
    # Total amount sold from manual override inventory by the bot (raw units).
    manual_override_sold_raw: Optional[str] = None
    # Derived/runtime-only field: current tradable manual override inventory (raw units).
    manual_override_tradable_raw: Optional[str] = None
    # Manual override reconciliation bypass: when active, only manual-override inventory may be tradable
    # while tx-proven reconciliation mismatch remains visible.
    manual_override_bypass_active: bool = False
    manual_override_bypass_reason: Optional[str] = None


class DiscoveredCandidateRecord(BaseModel):
    """
    Persisted snapshot of a discovered candidate — accepted or rejected.

    Written to discovery_recent_candidates (accepted) or discovery_rejected_candidates (rejected)
    in RuntimeState. Never mutated after creation; outcome is set once at decision time.
    """

    record_id: str
    mint: str
    source_id: str  # "pumpfun" | "watchlist" | "whale_copy" | "test" | ...
    source_confidence: float  # 0.0–1.0; source-assigned reliability hint
    discovered_at: datetime
    symbol: Optional[str] = None
    liquidity_usd: Optional[float] = None
    deployer: Optional[str] = None
    metadata_blob: Dict = Field(default_factory=dict)
    metadata_truncated: bool = False  # True if metadata_blob was truncated at record creation
    score: Optional[float] = None
    score_breakdown: Dict = Field(default_factory=dict)  # per-dimension scores; e.g. {"liquidity": 0.25, "whale_signal": 0.40}
    outcome: Literal["pending", "accepted", "rejected", "enqueued"] = "pending"
    rejection_reason: Optional[str] = None  # stable reason code from token_filter / pipeline
    processed_at: Optional[datetime] = None
    # Provenance — discovery origin
    discovery_signals: Dict = Field(default_factory=dict)  # source-specific signals, e.g. trigger_wallet for whale_copy
    enrichment_data: Dict = Field(default_factory=dict)  # on-chain enrichment: authority_ok, holder_top10_pct, etc.
    # Approval lineage
    approval_path: Optional[str] = None  # "auto" | "operator_manual" | None
    operator_approved_at: Optional[datetime] = None
    operator_approved_by: Optional[str] = None
    enqueue_source: Optional[str] = None  # "discovery_auto" | "discovery_operator_approval" | "manual_seed"


class DiscoveryStats(BaseModel):
    """Counters for the discovery pipeline — persisted per session."""

    total_discovered: int = 0
    total_accepted: int = 0
    total_rejected: int = 0
    total_enqueued: int = 0
    by_source: Dict[str, int] = Field(default_factory=dict)
    by_rejection_reason: Dict[str, int] = Field(default_factory=dict)
    # Per-source sub-stats: source_id -> {discovered, accepted, rejected, enqueued}
    source_stats: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    # Enrichment stats
    enrichment_checks_run: int = 0
    enrichment_partial_count: int = 0
    enrichment_hard_reject_count: int = 0


class SniperManualSeedQueueEntry(BaseModel):
    """Manual-seed sniper queue entry (Phase 1: manual_seed source only)."""

    mint: str
    enqueued_at: int
    source: str = "manual_seed"
    note: Optional[str] = None


SniperAttemptState = Literal[
    "created",
    "quoted",
    "submitted",
    "pending_chain_observation",
    "observed_candidate_receipt",
    "quote_rejected",
    "resolved_success",
    "resolved_failed",
    "resolved_uncertain",
]


SniperDecisionOutcome = Literal[
    "validation_rejected",
    "duplicate_blocked",
    "cooldown_blocked",
    "risk_blocked",
    "score_blocked",
    "quote_rejected",
    "buy_submitted",
    "buy_confirmed",
    "buy_failed",
    "buy_uncertain",
]


class SniperCooldownEntry(BaseModel):
    mint: str
    last_attempt_at: Optional[int] = None
    last_success_at: Optional[int] = None
    last_full_exit_at: Optional[int] = None


class SniperDecisionEntry(BaseModel):
    ts: int
    mint: str
    symbol: Optional[str] = None
    outcome: SniperDecisionOutcome
    discovery_source: Optional[str] = None
    score_total: Optional[float] = None
    reason_codes: List[str] = Field(default_factory=list)


class SniperAttempt(BaseModel):
    attempt_id: str
    candidate_id: str
    mint: str
    symbol: Optional[str] = None
    discovery_source: str
    state: SniperAttemptState
    score_total: Optional[float] = None
    requested_buy_sol: Optional[str] = None
    created_at: int
    quoted_at: Optional[int] = None
    submitted_at: Optional[int] = None
    signature: Optional[str] = None
    resolution_reason: Optional[str] = None
    confirmed_via: Optional[str] = None
    strategy_id: Optional[str] = None


class SniperStats(BaseModel):
    total_candidates_seen: int = 0
    total_candidates_blocked_risk: int = 0
    total_candidates_blocked_duplicate: int = 0
    total_candidates_blocked_cooldown: int = 0
    total_attempts: int = 0
    total_successful_attempts: int = 0
    total_failed_attempts: int = 0
    total_uncertain_attempts: int = 0
    total_quote_rejected_attempts: int = 0


class RuntimeState(BaseModel):
    version: int = 1
    started_at: datetime
    status_file: str
    # Optional wallet and SOL balance snapshot (used by dashboard UI).
    wallet: Optional[str] = None
    sol: Optional[SolBalance] = None
    mints: Dict[str, RuntimeMintState] = Field(default_factory=dict)
    # Session summary: SOL at session start (set on first run after load)
    session_start_sol: Optional[float] = None
    # Idempotency: token→token source disposals already applied (sig|source_mint); prevents double-debit on replay.
    processed_token_to_token_disposals: List[str] = Field(default_factory=list)
    # Sniper Phase 1: live sniper runtime state (manual-seed sniper only; paper mode is runtime-only and not persisted here).
    sniper_pending_attempts: Dict[str, SniperAttempt] = Field(default_factory=dict)
    sniper_attempt_history: List[SniperAttempt] = Field(default_factory=list)
    sniper_last_decisions: List[SniperDecisionEntry] = Field(default_factory=list)
    sniper_candidate_cooldowns: Dict[str, SniperCooldownEntry] = Field(default_factory=dict)
    sniper_recent_success_timestamps_hour: List[int] = Field(default_factory=list)
    sniper_recent_success_timestamps_day: List[int] = Field(default_factory=list)
    sniper_manual_seed_queue: List[SniperManualSeedQueueEntry] = Field(default_factory=list)
    sniper_stats: SniperStats = Field(default_factory=SniperStats)
    processed_sniper_signatures: List[str] = Field(default_factory=list)
    # Discovery phase: bounded history of discovered / rejected candidates + counters.
    discovery_recent_candidates: List[DiscoveredCandidateRecord] = Field(default_factory=list)
    discovery_rejected_candidates: List[DiscoveredCandidateRecord] = Field(default_factory=list)
    discovery_stats: DiscoveryStats = Field(default_factory=DiscoveryStats)

