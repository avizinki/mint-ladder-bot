"""
Discovery enrichment layer.

Runs after source fetch and token_filter, before scoring.
Performs on-chain checks: authority, holder concentration, LP lock, liquidity refresh.

Safety rules:
- ALL RPC/API calls are wrapped in try/except with per-call timeout.
- Failures produce "unavailable" or "unknown" status fields — never hard-block on failure.
- Hard-block filters only trigger on CONFIRMED positive risk (not on timeouts/errors).
- Partial enrichment emits ENRICHMENT_PARTIAL log event and continues.
- In-memory per-cycle cache prevents redundant RPC calls for the same mint.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Enrichment status constants
STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNKNOWN = "unknown"
STATUS_RISK = "risk"

# Hard-block rejection reason codes (stable, used in dashboard/pipeline)
REASON_HOLDER_CONCENTRATION_RISK = "holder_concentration_risk"
REASON_LP_UNLOCK_RISK = "lp_unlock_risk"

# Default thresholds
DEFAULT_HOLDER_TOP10_MAX_PCT = 80.0  # reject if top-10 holders own > 80%
DEFAULT_ENRICH_CALL_TIMEOUT_S = 5.0
DEFAULT_ENRICH_TOTAL_TIMEOUT_S = 10.0


class EnrichmentResult:
    """
    Result of enriching a candidate.

    hard_block: if True, pipeline should reject candidate immediately.
    rejection_reason: stable reason code when hard_block=True.
    data: dict of enrichment fields to merge into record.enrichment_data.
    partial: True if one or more checks were unavailable due to failure/timeout.
    """

    __slots__ = ("hard_block", "rejection_reason", "data", "partial")

    def __init__(
        self,
        hard_block: bool = False,
        rejection_reason: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        partial: bool = False,
    ) -> None:
        self.hard_block = hard_block
        self.rejection_reason = rejection_reason
        self.data: Dict[str, Any] = data or {}
        self.partial = partial


class CandidateEnricher:
    """
    Stateless enricher; instantiated once per process with config.
    Per-cycle cache is passed in by the pipeline per run() call.
    """

    def __init__(
        self,
        rpc: Any = None,
        holder_top10_max_pct: float = DEFAULT_HOLDER_TOP10_MAX_PCT,
        call_timeout_s: float = DEFAULT_ENRICH_CALL_TIMEOUT_S,
        total_timeout_s: float = DEFAULT_ENRICH_TOTAL_TIMEOUT_S,
        lp_unlock_risk_enabled: bool = False,  # disabled by default — stage 5
        holder_concentration_enabled: bool = False,  # disabled by default — stage 5
    ) -> None:
        self.rpc = rpc
        self.holder_top10_max_pct = holder_top10_max_pct
        self.call_timeout_s = call_timeout_s
        self.total_timeout_s = total_timeout_s
        self.lp_unlock_risk_enabled = lp_unlock_risk_enabled
        self.holder_concentration_enabled = holder_concentration_enabled

    def enrich(
        self,
        mint: str,
        candidate_liquidity_usd: Optional[float],
        cycle_cache: Dict[str, EnrichmentResult],
    ) -> EnrichmentResult:
        """
        Enrich a single mint. Results are cached per-cycle to avoid redundant RPC calls.

        Returns EnrichmentResult. Never raises — all failures are soft.
        """
        if mint in cycle_cache:
            return cycle_cache[mint]

        deadline = time.monotonic() + self.total_timeout_s
        data: Dict[str, Any] = {}
        partial = False

        # --- Authority check ---
        if self.rpc is not None and time.monotonic() < deadline:
            auth_status, auth_detail = self._check_authority(mint, deadline)
            data["authority_check"] = auth_status
            if auth_detail:
                data["authority_detail"] = auth_detail
            if auth_status == STATUS_UNAVAILABLE:
                partial = True
            elif auth_status == STATUS_RISK:
                result = EnrichmentResult(
                    hard_block=True,
                    rejection_reason=auth_detail or "mint_authority_risk",
                    data=data,
                    partial=partial,
                )
                cycle_cache[mint] = result
                return result
        else:
            data["authority_check"] = STATUS_UNAVAILABLE
            if self.rpc is not None:
                partial = True  # had rpc but timed out

        # --- Holder concentration check ---
        if self.holder_concentration_enabled and self.rpc is not None and time.monotonic() < deadline:
            top10_pct, holder_status = self._check_holder_concentration(mint, deadline)
            data["holder_concentration_check"] = holder_status
            if top10_pct is not None:
                data["holder_top10_pct"] = top10_pct
            if holder_status == STATUS_UNAVAILABLE:
                partial = True
            elif holder_status == STATUS_RISK and top10_pct is not None:
                result = EnrichmentResult(
                    hard_block=True,
                    rejection_reason=REASON_HOLDER_CONCENTRATION_RISK,
                    data=data,
                    partial=partial,
                )
                cycle_cache[mint] = result
                return result
        else:
            data["holder_concentration_check"] = STATUS_UNKNOWN

        # --- LP lock check ---
        if self.lp_unlock_risk_enabled and time.monotonic() < deadline:
            lp_status = self._check_lp_lock(mint, candidate_liquidity_usd, deadline)
            data["lp_lock_status"] = lp_status
            if lp_status == STATUS_UNAVAILABLE:
                partial = True
            elif lp_status == STATUS_RISK:
                result = EnrichmentResult(
                    hard_block=True,
                    rejection_reason=REASON_LP_UNLOCK_RISK,
                    data=data,
                    partial=partial,
                )
                cycle_cache[mint] = result
                return result
        else:
            data["lp_lock_status"] = STATUS_UNKNOWN

        if partial:
            logger.debug("ENRICHMENT_PARTIAL mint=%s data=%s", mint[:12], list(data.keys()))

        result = EnrichmentResult(hard_block=False, data=data, partial=partial)
        cycle_cache[mint] = result
        return result

    # ------------------------------------------------------------------
    # Private check helpers — each returns on failure, never raises
    # ------------------------------------------------------------------

    def _check_authority(self, mint: str, deadline: float) -> tuple[str, Optional[str]]:
        """
        Returns (status, detail). status: STATUS_OK | STATUS_RISK | STATUS_UNAVAILABLE
        Uses existing authority_checks module if available.
        """
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return STATUS_UNAVAILABLE, None
            from ..authority_checks import check_mint_authorities
            mint_ok, freeze_ok, err = check_mint_authorities(self.rpc, mint)
            if not mint_ok:
                return STATUS_RISK, "mint_authority_risk"
            if not freeze_ok:
                return STATUS_RISK, "freeze_authority_risk"
            return STATUS_OK, None
        except Exception as e:
            logger.debug("enrichment authority_check failed mint=%s err=%s", mint[:12], str(e)[:100])
            return STATUS_UNAVAILABLE, None

    def _check_holder_concentration(self, mint: str, deadline: float) -> tuple[Optional[float], str]:
        """
        Returns (top10_pct_or_None, status).
        Placeholder — real implementation requires token accounts RPC call.
        Returns (None, STATUS_UNAVAILABLE) until implemented.
        """
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, STATUS_UNAVAILABLE
            # Placeholder: real implementation would call getTokenLargestAccounts RPC
            # and compute top-10 concentration. Returns unavailable until implemented.
            return None, STATUS_UNAVAILABLE
        except Exception as e:
            logger.debug("enrichment holder_check failed mint=%s err=%s", mint[:12], str(e)[:100])
            return None, STATUS_UNAVAILABLE

    def _check_lp_lock(self, mint: str, liquidity_usd: Optional[float], deadline: float) -> str:
        """
        Returns status: STATUS_OK | STATUS_RISK | STATUS_UNAVAILABLE | STATUS_UNKNOWN
        Placeholder — real implementation requires LP pool lookup.
        """
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return STATUS_UNAVAILABLE
            # Placeholder: real implementation would check LP token lock state
            return STATUS_UNKNOWN
        except Exception as e:
            logger.debug("enrichment lp_lock_check failed mint=%s err=%s", mint[:12], str(e)[:100])
            return STATUS_UNAVAILABLE


def make_enricher_from_config(config: Any) -> CandidateEnricher:
    """Construct enricher from Config object."""
    rpc = getattr(config, "_rpc_client", None)  # injected by runner if available
    return CandidateEnricher(
        rpc=rpc,
        holder_top10_max_pct=getattr(config, "discovery_holder_top10_max_pct", DEFAULT_HOLDER_TOP10_MAX_PCT),
        call_timeout_s=getattr(config, "discovery_enrichment_call_timeout_s", DEFAULT_ENRICH_CALL_TIMEOUT_S),
        total_timeout_s=getattr(config, "discovery_enrichment_total_timeout_s", DEFAULT_ENRICH_TOTAL_TIMEOUT_S),
        lp_unlock_risk_enabled=getattr(config, "discovery_lp_unlock_risk_enabled", False),
        holder_concentration_enabled=getattr(config, "discovery_holder_concentration_enabled", False),
    )
