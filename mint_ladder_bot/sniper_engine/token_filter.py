"""
Token filter: reject launch candidates by liquidity, mint authority, metadata, deployer, scam patterns.

Output: FilterResult (pass/reject, reason). Used before sniper decision.
Reason codes are stable. score_breakdown: authority_ok, deployer_ok, metadata_ok, liquidity_ok.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .launch_detector import LaunchCandidate

# Stable reject reason codes (observability / dashboard)
REASON_OK = "ok"
REASON_BLOCKLIST = "blocklist"
REASON_MISSING_METADATA = "missing_metadata"
REASON_LIQUIDITY_BELOW_THRESHOLD = "liquidity_below_threshold"
REASON_MINT_AUTHORITY_RISK = "mint_authority_risk"
REASON_FREEZE_AUTHORITY_RISK = "freeze_authority_risk"
REASON_SCAM_PATTERN = "scam_pattern"
REASON_SLIPPAGE_RISK = "slippage_risk"
REASON_OTHER = "other"


@dataclass
class FilterResult:
    """Result of filtering a launch candidate."""

    passed: bool
    reason: str  # one of REASON_* constants
    details: Optional[Dict[str, Any]] = None
    score_breakdown: Optional[Dict[str, Any]] = None  # optional scored model breakdown


def filter_candidate(
    candidate: LaunchCandidate,
    min_liquidity_usd: float = 5_000.0,
    require_metadata: bool = True,
    blocklist_mints: Optional[set[str]] = None,
    scam_patterns_enabled: bool = True,
    rpc: Any = None,
    deployer_history_path: Optional[Path] = None,
) -> FilterResult:
    """
    Apply filter rules. Reject if: blocklist, missing metadata, liquidity, mint/freeze authority, deployer reputation.
    When rpc provided, checks mint_authority and freeze_authority. When deployer_history_path provided, checks deployer ratio.
    """
    blocklist_mints = blocklist_mints or set()
    if candidate.mint in blocklist_mints:
        return FilterResult(False, REASON_BLOCKLIST, {"mint": candidate.mint[:12]})

    metadata = candidate.metadata or {}
    metadata_ok = not require_metadata or bool(metadata.get("symbol") or metadata.get("name"))
    if require_metadata and not metadata.get("symbol") and not metadata.get("name"):
        return FilterResult(False, REASON_MISSING_METADATA, {"mint": candidate.mint[:12]})

    liquidity_usd = None
    if isinstance(metadata.get("liquidity_usd"), (int, float)):
        liquidity_usd = float(metadata["liquidity_usd"])
    if liquidity_usd is not None and liquidity_usd < min_liquidity_usd:
        return FilterResult(
            False,
            REASON_LIQUIDITY_BELOW_THRESHOLD,
            {"liquidity_usd": liquidity_usd, "min": min_liquidity_usd},
        )

    authority_ok = True
    if rpc is not None:
        from .authority_checks import check_mint_authorities
        mint_ok, freeze_ok, err = check_mint_authorities(rpc, candidate.mint)
        authority_ok = mint_ok and freeze_ok
        if not mint_ok:
            return FilterResult(False, REASON_MINT_AUTHORITY_RISK, {"mint": candidate.mint[:12], "detail": err})
        if not freeze_ok:
            return FilterResult(False, REASON_FREEZE_AUTHORITY_RISK, {"mint": candidate.mint[:12], "detail": err})

    deployer_ok = True
    deployer = (metadata.get("deployer") or "").strip()
    if deployer and deployer_history_path is not None:
        from .deployer_reputation import is_deployer_acceptable
        deployer_ok = is_deployer_acceptable(deployer_history_path, deployer)
        if not deployer_ok:
            return FilterResult(False, REASON_SCAM_PATTERN, {"mint": candidate.mint[:12], "reason": "deployer_reject_ratio"})

    if scam_patterns_enabled:
        pass  # placeholder for future scam checks

    score_breakdown: Dict[str, Any] = {
        "authority_ok": authority_ok,
        "deployer_ok": deployer_ok,
        "metadata_ok": metadata_ok,
        "liquidity_ok": liquidity_usd is None or liquidity_usd >= min_liquidity_usd,
    }
    if liquidity_usd is not None:
        score_breakdown["liquidity_usd"] = liquidity_usd
    return FilterResult(
        True,
        REASON_OK,
        {"mint": candidate.mint[:12]},
        score_breakdown=score_breakdown,
    )
