#!/usr/bin/env python3
"""
Full source-wallet provenance report for all transferred tokens in trading wallet (read-only).

Scope:
- Source wallet: 9T6wvKnUiQDctcE8DyN8kfMxQcchqJzFQNiXfvYvU1fY
- Trading wallet: 3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR

For every token currently in the trading wallet that received inventory via transfer
from the source wallet, this tool:

1. Aggregates transfer information (signatures, times, slots, total amount transferred)
   from source_wallet_provenance_report.json.
2. Merges deep acquisition / entry-price estimates from source_wallet_provenance_deep_report.json
   and earlier FIFO provenance (when available).
3. Joins current reconciliation status and blocker category in the trading wallet.
4. Assigns a recommended future action per mint.
5. Writes a machine-readable JSON report and a human-readable markdown summary.

Constraints:
- Read-only only: no mutation of state.json or status.json.
- No lot creation, no runtime restart.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _load_env() -> None:
    for p in (_REPO / ".env", Path(".env")):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()


_load_env()

SOURCE_WALLET = os.environ.get(
    "SOURCE_WALLET",
    "9T6wvKnUiQDctcE8DyN8kfMxQcchqJzFQNiXfvYvU1fY",
)
TRADING_WALLET = os.environ.get(
    "TRADING_WALLET",
    "3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR",
)

DATA_DIR = _REPO / "runtime" / "projects" / "mint_ladder_bot"
STATE_PATH = DATA_DIR / "state.json"
STATUS_PATH = DATA_DIR / "status.json"
BASE_PROVENANCE_JSON = DATA_DIR / "source_wallet_provenance_report.json"
DEEP_PROVENANCE_JSON = DATA_DIR / "source_wallet_provenance_deep_report.json"
FULL_JSON_PATH = DATA_DIR / "source_wallet_full_trading_wallet_report.json"
FULL_MD_PATH = DATA_DIR / "source_wallet_full_trading_wallet_report.md"


@dataclass
class TransferInfo:
    signature: str
    slot: Optional[int]
    time: Optional[str]
    amount_raw: int


@dataclass
class FullMintReport:
    mint: str
    symbol: Optional[str]
    wallet_balance_raw: int
    total_transferred_raw: int
    transfers: List[TransferInfo]
    best_source_acquisition_sig: Optional[str]
    acquisition_time: Optional[str]
    acquisition_method: str
    entry_price_estimated: Optional[float]
    sol_spent_estimated: Optional[float]
    tokens_received_raw: Optional[int]
    confidence_score: str
    confidence_reason: str
    supporting_evidence_summary: str
    reconciliation_status: Optional[str]
    blocker_category: Optional[str]
    recommended_future_action: str

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["transfers"] = [asdict(t) for t in self.transfers]
        return d


def _short_conf_label(conf: str) -> str:
    conf = (conf or "").upper()
    if conf.startswith("HIGH"):
        return "HIGH"
    if conf.startswith("MEDIUM"):
        return "MEDIUM"
    if conf.startswith("LOW"):
        return "LOW"
    return "LOW"


def main() -> int:
    from mint_ladder_bot.models import StatusFile
    from mint_ladder_bot.state import load_state
    from mint_ladder_bot.reconciliation_report import compute_reconciliation_records

    if not BASE_PROVENANCE_JSON.exists():
        print(
            f"Base provenance report not found at {BASE_PROVENANCE_JSON}",
            file=sys.stderr,
        )
        return 1
    if not DEEP_PROVENANCE_JSON.exists():
        print(
            f"Deep provenance report not found at {DEEP_PROVENANCE_JSON}",
            file=sys.stderr,
        )
        return 1
    if not STATUS_PATH.exists():
        print("status.json not found; run status snapshot first.", file=sys.stderr)
        return 1

    base = json.loads(BASE_PROVENANCE_JSON.read_text())
    deep = json.loads(DEEP_PROVENANCE_JSON.read_text())
    base_rows = base.get("rows") or []
    deep_results = deep.get("results") or []

    status = StatusFile.model_validate_json(STATUS_PATH.read_text())
    state = load_state(STATE_PATH, STATUS_PATH) if STATE_PATH.exists() else None

    # Maps from status for balance / decimals / symbol.
    decimals_by_mint: Dict[str, int] = {}
    symbol_by_mint: Dict[str, Optional[str]] = {}
    balance_raw_by_mint: Dict[str, int] = {}
    for m in status.mints:
        decimals_by_mint[m.mint] = getattr(m, "decimals", 6)
        symbol_by_mint[m.mint] = getattr(m, "symbol", None)
        try:
            balance_raw_by_mint[m.mint] = int(getattr(m, "balance_raw", 0) or 0)
        except (ValueError, TypeError):
            balance_raw_by_mint[m.mint] = 0

    # Reconciliation records for trading wallet.
    rec_by_mint: Dict[str, Any] = {}
    if state is not None:
        recs = compute_reconciliation_records(state, status)
        rec_by_mint = {r.mint: r for r in recs}

    # Group base provenance transfers by mint.
    transfers_by_mint: Dict[str, List[TransferInfo]] = {}
    base_meta_by_mint: Dict[str, Dict[str, Any]] = {}
    for r in base_rows:
        mint = r.get("mint")
        if not mint:
            continue
        try:
            amt = int(r.get("amount_transferred_raw", r.get("amount_raw", 0)) or 0)
        except (ValueError, TypeError):
            amt = 0
        ti = TransferInfo(
            signature=r.get("transfer_sig") or r.get("tx_signature") or "",
            slot=r.get("transfer_slot"),
            time=r.get("transfer_time")
            or r.get("block_time"),  # base report uses block_time
            amount_raw=amt,
        )
        transfers_by_mint.setdefault(mint, []).append(ti)
        base_meta_by_mint[mint] = r

    # Map deep provenance by mint.
    deep_by_mint: Dict[str, Dict[str, Any]] = {
        r.get("mint"): r for r in deep_results if r.get("mint")
    }

    full_reports: List[FullMintReport] = []

    for mint, transfers in transfers_by_mint.items():
        symbol = symbol_by_mint.get(mint)
        wallet_balance_raw = balance_raw_by_mint.get(mint, 0)
        total_transferred_raw = sum(t.amount_raw for t in transfers)

        base_meta = base_meta_by_mint.get(mint, {})
        deep_meta = deep_by_mint.get(mint, {})

        # Best acquisition info: prefer deep, but override with FIFO-based HIGH from base when present.
        deep_method = deep_meta.get("acquisition_method") or ""
        deep_ep = deep_meta.get("entry_price_estimated")
        deep_conf = deep_meta.get("confidence_score") or "LOW_CONFIDENCE"
        deep_reason = deep_meta.get("confidence_reason") or ""
        deep_sig = deep_meta.get("best_source_acquisition_sig")
        deep_time = deep_meta.get("acquisition_time")

        base_method = base_meta.get("method") or ""
        base_conf = base_meta.get("confidence_score") or ""
        base_ep = base_meta.get("entry_price_estimated")
        base_sol_spent = base_meta.get("sol_spent")
        base_tokens = base_meta.get("tokens_received_raw")
        base_swap_sig = base_meta.get("source_swap_sig")

        # Default: use deep.
        acquisition_method = deep_method or "unknown"
        entry_price_estimated = deep_ep
        confidence_score = _short_conf_label(deep_conf)
        confidence_reason = deep_reason or "deep_provenance"
        best_acq_sig = deep_sig
        acq_time = deep_time
        sol_spent_estimated: Optional[float] = None
        tokens_received_raw: Optional[int] = None

        # If we had a strong FIFO-based provenance earlier, prefer that.
        if base_method == "fifo_source_lots" and base_conf.startswith("HIGH"):
            acquisition_method = "fifo_source_lots"
            entry_price_estimated = base_ep
            confidence_score = "HIGH"
            confidence_reason = "fifo_source_lots + tx-proven source lots"
            best_acq_sig = base_swap_sig or deep_sig
            acq_time = deep_time or base_meta.get("transfer_time")
            sol_spent_estimated = base_sol_spent
            tokens_received_raw = base_tokens
        else:
            # Otherwise, estimate sol_spent from entry price when possible.
            tokens_received_raw = total_transferred_raw
            dec = decimals_by_mint.get(mint, 6)
            if (
                entry_price_estimated is not None
                and total_transferred_raw > 0
                and dec >= 0
            ):
                token_human = total_transferred_raw / (10 ** dec)
                sol_spent_estimated = entry_price_estimated * token_human

        # Supporting evidence summary.
        evidence_bits: List[str] = []
        if base_method:
            evidence_bits.append(f"base_method={base_method}")
        if deep_method:
            evidence_bits.append(f"deep_method={deep_method}")
        if base_conf:
            evidence_bits.append(f"base_conf={base_conf}")
        if deep_conf:
            evidence_bits.append(f"deep_conf={deep_conf}")
        supporting_evidence_summary = "; ".join(evidence_bits) if evidence_bits else ""

        # Reconciliation side (trading wallet).
        rec = rec_by_mint.get(mint)
        reconciliation_status = (
            rec.reconciliation_status if rec is not None else None
        )
        blocker_category = rec.blocker_category if rec is not None else None

        # Recommended future action.
        # Heuristic:
        # - If reconciliation_status == "sufficient": already_safe
        # - Else if confidence HIGH and acquisition_method in (fifo_source_lots, explicit_swap, inferred_swap_from_sol_delta):
        #       candidate_for_source_wallet_proven_lot
        # - Else if confidence MEDIUM:
        #       candidate_for_manual_override_only
        # - Else:
        #       insufficient_evidence
        rec_status = (reconciliation_status or "").lower()
        conf_short = confidence_score

        if rec_status == "sufficient":
            recommended = "already_safe"
        elif conf_short == "HIGH" and acquisition_method in (
            "fifo_source_lots",
            "explicit_swap",
            "inferred_swap_from_sol_delta",
        ):
            recommended = "candidate_for_source_wallet_proven_lot"
        elif conf_short == "MEDIUM":
            recommended = "candidate_for_manual_override_only"
        else:
            recommended = "insufficient_evidence"

        full_reports.append(
            FullMintReport(
                mint=mint,
                symbol=symbol,
                wallet_balance_raw=wallet_balance_raw,
                total_transferred_raw=total_transferred_raw,
                transfers=transfers,
                best_source_acquisition_sig=best_acq_sig,
                acquisition_time=acq_time,
                acquisition_method=acquisition_method or "unknown",
                entry_price_estimated=entry_price_estimated,
                sol_spent_estimated=sol_spent_estimated,
                tokens_received_raw=tokens_received_raw,
                confidence_score=confidence_score,
                confidence_reason=confidence_reason,
                supporting_evidence_summary=supporting_evidence_summary,
                reconciliation_status=reconciliation_status,
                blocker_category=blocker_category,
                recommended_future_action=recommended,
            )
        )

    # Summary counts.
    total_tokens = len(full_reports)
    by_conf: Dict[str, int] = {}
    by_action: Dict[str, int] = {}
    for r in full_reports:
        by_conf[r.confidence_score] = by_conf.get(r.confidence_score, 0) + 1
        by_action[r.recommended_future_action] = (
            by_action.get(r.recommended_future_action, 0) + 1
        )

    # Strongest and weakest tokens.
    strongest = sorted(
        full_reports,
        key=lambda r: (
            0 if r.confidence_score == "HIGH" else (1 if r.confidence_score == "MEDIUM" else 2),
            0 if r.recommended_future_action
            == "candidate_for_source_wallet_proven_lot"
            else 1,
        ),
    )
    weakest = sorted(
        full_reports,
        key=lambda r: (
            2 if r.confidence_score == "HIGH" else (1 if r.confidence_score == "MEDIUM" else 0),
        ),
        reverse=False,
    )

    json_report = {
        "source_wallet": SOURCE_WALLET,
        "trading_wallet": TRADING_WALLET,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_tokens_with_provenance": total_tokens,
        "counts_by_confidence": by_conf,
        "counts_by_recommended_action": by_action,
        "tokens": [r.to_dict() for r in full_reports],
        "strongest_tokens": [
            {
                "mint": r.mint,
                "symbol": r.symbol,
                "confidence_score": r.confidence_score,
                "recommended_future_action": r.recommended_future_action,
            }
            for r in strongest[:5]
        ],
        "weakest_tokens": [
            {
                "mint": r.mint,
                "symbol": r.symbol,
                "confidence_score": r.confidence_score,
                "recommended_future_action": r.recommended_future_action,
            }
            for r in weakest[:5]
        ],
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FULL_JSON_PATH.write_text(json.dumps(json_report, indent=2), encoding="utf-8")

    # Markdown summary.
    lines: List[str] = []
    lines.append("# Full source-wallet trading-wallet provenance report")
    lines.append("")
    lines.append(f"- Source wallet: `{SOURCE_WALLET}`")
    lines.append(f"- Trading wallet: `{TRADING_WALLET}`")
    lines.append(f"- Generated at: {json_report['generated_at']}")
    lines.append(
        f"- Total tokens with provenance from source wallet: {total_tokens}"
    )
    lines.append("")
    lines.append("## Counts by confidence")
    for k in sorted(by_conf.keys()):
        lines.append(f"- **{k}**: {by_conf[k]}")
    lines.append("")
    lines.append("## Counts by recommended future action")
    for k in sorted(by_action.keys()):
        lines.append(f"- **{k}**: {by_action[k]}")
    lines.append("")
    if strongest:
        lines.append("## Strongest provenance tokens")
        for r in strongest[:5]:
            lines.append(
                f"- **{r.symbol or r.mint[:8]}** ({r.mint}): confidence={r.confidence_score}, action={r.recommended_future_action}"
            )
        lines.append("")
    if weakest:
        lines.append("## Weakest / most uncertain tokens")
        for r in weakest[:5]:
            lines.append(
                f"- **{r.symbol or r.mint[:8]}** ({r.mint}): confidence={r.confidence_score}, action={r.recommended_future_action}"
            )
        lines.append("")

    FULL_MD_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Full JSON report written to {FULL_JSON_PATH}")
    print(f"Full Markdown report written to {FULL_MD_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

