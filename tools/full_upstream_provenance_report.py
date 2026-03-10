#!/usr/bin/env python3
"""
Full upstream provenance reconstruction for trading wallet (read-only).

Trading wallet: 3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR

For every mint currently held in the trading wallet, this tool:

- Scans trading-wallet history for positive token deltas (direct swaps and transfers-in).
- Partitions inbound inventory into:
  - direct_proven_raw: tx-derived swaps in the trading wallet
  - per-source-wallet transfer contributions
- For each (mint, source_wallet) contribution, performs a one-hop upstream
  scratch reconstruction of the source wallet to estimate how much of that
  contribution is tx-proven vs inferred-only.
- Aggregates per-mint:
  - direct_proven_raw
  - upstream_proven_raw
  - inferred_only_raw
  - unresolved_raw
- Computes a best-entry method, weighted entry-price estimate (when possible),
  provenance confidence, and recommended action.
- Also builds a source-wallet contribution summary table.

Constraints:
- Read-only only: no mutation of state.json or status.json.
- No lot creation, no runtime restart.
- Recursion depth: 1 hop (source wallet only).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

TRADING_WALLET = os.environ.get(
    "TRADING_WALLET",
    "3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR",
)

DATA_DIR = _REPO / "runtime" / "projects" / "mint_ladder_bot"
STATE_PATH = DATA_DIR / "state.json"
STATUS_PATH = DATA_DIR / "status.json"

JSON_REPORT_PATH = DATA_DIR / "full_upstream_provenance_report.json"
MD_REPORT_PATH = DATA_DIR / "full_upstream_provenance_report.md"

MAX_WALLETS_PER_MINT = 8


def _dexscreener_price_native(http_client: Any, mint: str) -> Optional[float]:
    """
    Best-effort Dexscreener price_native fetch (current pool price).
    Returns SOL per token (float) or None.
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        resp = http_client.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None
        best = None
        best_liq = -1.0
        for p in pairs:
            liq = (p.get("liquidity") or {}).get("usd")
            try:
                liq_f = float(liq) if liq is not None else 0.0
            except (ValueError, TypeError):
                liq_f = 0.0
            if liq_f > best_liq:
                best_liq = liq_f
                best = p
        if not best:
            return None
        price_native = best.get("priceNative")
        if price_native is None:
            return None
        return float(price_native)
    except Exception:
        return None


@dataclass
class PerMintProvenance:
    mint: str
    symbol: Optional[str]
    current_wallet_balance_raw: int
    direct_proven_raw: int
    upstream_proven_raw: int
    inferred_only_raw: int
    unresolved_raw: int
    source_wallet_count: int
    source_wallets: List[str]
    best_entry_method: str
    weighted_entry_price_estimate: Optional[float]
    provenance_confidence: str
    current_reconciliation_status: Optional[str]
    current_blocker_category: Optional[str]
    recommended_action: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SourceWalletSummary:
    source_wallet: str
    mints_contributed: List[str]
    total_transfers_raw: int
    highest_confidence: str
    lowest_confidence: str
    overall_usefulness_score: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


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
    from httpx import Client as HttpClient

    from mint_ladder_bot.config import Config
    from mint_ladder_bot.models import StatusFile
    from mint_ladder_bot.reconciliation_report import compute_reconciliation_records
    from mint_ladder_bot.rpc import RpcClient
    from mint_ladder_bot.state import load_state
    from mint_ladder_bot.transfer_provenance_analysis import (
        CLASS_LIKELY_SWAP,
        run_transfer_provenance_analysis,
    )
    from mint_ladder_bot.transfer_provenance_scratch import (
        _provenance_valid_lots_ordered,
        run_source_wallet_scratch_reconstruction,
        _fifo_attribution,
    )

    if not STATUS_PATH.exists():
        print("status.json not found; run status snapshot first.", file=sys.stderr)
        return 1

    config = Config()
    status = StatusFile.model_validate_json(STATUS_PATH.read_text())
    if status.wallet != TRADING_WALLET:
        print(
            f"Warning: status wallet={status.wallet} differs from TRADING_WALLET={TRADING_WALLET}",
            file=sys.stderr,
        )

    state = load_state(STATE_PATH, STATUS_PATH) if STATE_PATH.exists() else None

    # Mints currently held in trading wallet (balance_raw > 0).
    mints_set = set()
    decimals_by_mint: Dict[str, int] = {}
    symbol_by_mint: Dict[str, Optional[str]] = {}
    balance_by_mint: Dict[str, int] = {}
    for m in status.mints:
        try:
            bal = int(getattr(m, "balance_raw", 0) or 0)
        except (ValueError, TypeError):
            bal = 0
        if bal <= 0:
            continue
        mints_set.add(m.mint)
        decimals_by_mint[m.mint] = getattr(m, "decimals", 6)
        symbol_by_mint[m.mint] = getattr(m, "symbol", None)
        balance_by_mint[m.mint] = bal

    if not mints_set:
        print("No mints with positive balance in trading wallet.", file=sys.stderr)
        return 0

    rpc = RpcClient(config.rpc_endpoint, timeout_s=config.rpc_timeout_s)
    http_client = HttpClient()

    max_sigs = getattr(config, "reconstruction_max_signatures_per_wallet", 500)

    # Step 1: use transfer_provenance_analysis to scan trading wallet history.
    candidates = run_transfer_provenance_analysis(
        wallet=TRADING_WALLET,
        mints_tracked=mints_set,
        rpc=rpc,
        max_signatures=max_sigs,
        trusted_source_wallets=[],  # trust not required for forensic scan
        decimals_by_mint=decimals_by_mint,
        symbol_by_mint=symbol_by_mint,
        mint_filter=None,
    )

    # Aggregate direct swaps and per-source transfer contributions.
    direct_proven_by_mint: Dict[str, int] = {m: 0 for m in mints_set}
    contrib_by_mint_source: Dict[str, Dict[str, int]] = {
        m: {} for m in mints_set
    }

    for c in candidates:
        mint = getattr(c, "mint", None)
        if mint not in mints_set:
            continue
        amt = getattr(c, "amount_raw", 0) or 0
        try:
            amt_int = int(amt)
        except (ValueError, TypeError):
            continue
        if amt_int <= 0:
            continue
        classification = getattr(c, "classification", "")
        src_wallet = getattr(c, "source_wallet", None)

        if classification == CLASS_LIKELY_SWAP:
            # Direct swap in trading wallet.
            direct_proven_by_mint[mint] += amt_int
        else:
            if not src_wallet:
                continue
            per_src = contrib_by_mint_source.setdefault(mint, {})
            per_src[src_wallet] = per_src.get(src_wallet, 0) + amt_int

    # Step 2: upstream provenance per (mint, source_wallet).
    upstream_proven_by_mint: Dict[str, int] = {m: 0 for m in mints_set}
    inferred_only_by_mint: Dict[str, int] = {m: 0 for m in mints_set}

    # Cache for source scratch reconstructions to limit RPC.
    source_scratch_cache: Dict[Tuple[str, str], Tuple[Any, str]] = {}

    for mint in mints_set:
        per_src = contrib_by_mint_source.get(mint, {})
        if not per_src:
            continue

        for idx, (src_wallet, total_contrib_raw) in enumerate(per_src.items()):
            if idx >= MAX_WALLETS_PER_MINT:
                # Safety cap: skip additional wallets for this mint.
                inferred_only_by_mint[mint] += max(total_contrib_raw, 0)
                continue

            key = (src_wallet, mint)
            if key in source_scratch_cache:
                scratch_state, recon_status = source_scratch_cache[key]
            else:
                scratch_state, recon_status = run_source_wallet_scratch_reconstruction(
                    source_wallet=src_wallet,
                    mint=mint,
                    rpc=rpc,
                    max_signatures=max_sigs,
                    decimals_by_mint=decimals_by_mint,
                    symbol_by_mint=symbol_by_mint,
                )
                source_scratch_cache[key] = (scratch_state, recon_status)

            ms = scratch_state.mints.get(mint)
            lots_ordered = _provenance_valid_lots_ordered(ms) if ms else []
            if not lots_ordered or recon_status != "success":
                # No tx-proven upstream; treat entire contribution as inferred-only (if we later price via pool).
                inferred_only_by_mint[mint] += max(total_contrib_raw, 0)
                continue

            # Try FIFO attribution for the full contribution.
            dec = decimals_by_mint.get(mint, 6)
            attributed_raw, cost_sol, attr_ok = _fifo_attribution(
                lots_ordered, total_contrib_raw, dec
            )
            if not attr_ok or attributed_raw <= 0:
                inferred_only_by_mint[mint] += max(total_contrib_raw, 0)
                continue

            upstream_proven_by_mint[mint] += attributed_raw
            residual = total_contrib_raw - attributed_raw
            if residual > 0:
                inferred_only_by_mint[mint] += residual

    # Reconciliation records for current trading wallet.
    rec_by_mint: Dict[str, Any] = {}
    if state is not None:
        recs = compute_reconciliation_records(state, status)
        rec_by_mint = {r.mint: r for r in recs}

    # Step 3: assemble per-mint provenance summaries.
    per_mint_reports: List[PerMintProvenance] = []
    source_wallet_agg: Dict[str, Dict[str, Any]] = {}

    for mint in sorted(mints_set):
        bal = balance_by_mint.get(mint, 0)
        direct_proven = min(direct_proven_by_mint.get(mint, 0), bal)
        total_contrib_raw = sum(contrib_by_mint_source.get(mint, {}).values())
        upstream_proven = min(upstream_proven_by_mint.get(mint, 0), max(bal - direct_proven, 0))
        inferred_only = inferred_only_by_mint.get(mint, 0)

        # unresolved = balance - (direct + sum(contrib))
        unresolved = bal - (direct_proven + total_contrib_raw)
        if unresolved < 0:
            unresolved = 0

        symbol = symbol_by_mint.get(mint)

        # Source wallets set.
        src_wallets_list = sorted(contrib_by_mint_source.get(mint, {}).keys())
        source_wallet_count = len(src_wallets_list)

        # Estimate weighted entry price from upstream_proven and inferred_only (pool price),
        # ignoring direct_proven for now unless upstream missing.
        ep_weighted: Optional[float] = None
        total_cost_sol = 0.0
        total_tokens_for_price = 0

        # Upstream-proven cost: re-use cached scratch info and FIFO attribution for the upstream_proven portion.
        if upstream_proven > 0:
            # For simplicity: approximate using Dexscreener current price for mint.
            price_native = _dexscreener_price_native(http_client, mint)
            if price_native is not None:
                dec = decimals_by_mint.get(mint, 6)
                token_human = upstream_proven / (10 ** dec)
                total_cost_sol += token_human * price_native
                total_tokens_for_price += upstream_proven

        # Inferred-only portion: also via pool price.
        if inferred_only > 0:
            price_native = _dexscreener_price_native(http_client, mint)
            if price_native is not None:
                dec = decimals_by_mint.get(mint, 6)
                token_human = inferred_only / (10 ** dec)
                total_cost_sol += token_human * price_native
                total_tokens_for_price += inferred_only

        if total_tokens_for_price > 0:
            ep_weighted = total_cost_sol / (total_tokens_for_price / (10 ** decimals_by_mint.get(mint, 6)))

        # Reconciliation status and blocker.
        rec = rec_by_mint.get(mint)
        rec_status = rec.reconciliation_status if rec is not None else None
        blocker = rec.blocker_category if rec is not None else None

        # Provenance confidence and recommended action.
        proven_total = direct_proven + upstream_proven
        inferred_total = inferred_only
        unresolved_total = unresolved

        if proven_total >= bal * 0.98 and unresolved_total == 0:
            prov_conf = "HIGH"
        elif proven_total + inferred_total >= bal * 0.75:
            prov_conf = "MEDIUM"
        else:
            prov_conf = "LOW"

        rec_status_l = (rec_status or "").lower()
        if rec_status_l == "sufficient":
            recommended = "safe_to_resume"
        elif prov_conf == "HIGH" and upstream_proven > 0:
            recommended = "promote_from_upstream_provenance"
        elif prov_conf in ("HIGH", "MEDIUM") and inferred_total > 0:
            recommended = "manual_override_only"
        else:
            recommended = "unresolved_keep_blocked"

        per_mint_reports.append(
            PerMintProvenance(
                mint=mint,
                symbol=symbol,
                current_wallet_balance_raw=bal,
                direct_proven_raw=direct_proven,
                upstream_proven_raw=upstream_proven,
                inferred_only_raw=inferred_only,
                unresolved_raw=unresolved,
                source_wallet_count=source_wallet_count,
                source_wallets=src_wallets_list,
                best_entry_method=(
                    "direct_wallet_swap"
                    if direct_proven > 0
                    else ("source_wallet_fifo" if upstream_proven > 0 else ("fallback_pool_price" if inferred_only > 0 else "unresolved"))
                ),
                weighted_entry_price_estimate=ep_weighted,
                provenance_confidence=prov_conf,
                current_reconciliation_status=rec_status,
                current_blocker_category=blocker,
                recommended_action=recommended,
            )
        )

        # Aggregate per source wallet for summary.
        for src in src_wallets_list:
            amt = contrib_by_mint_source.get(mint, {}).get(src, 0)
            if amt <= 0:
                continue
            agg = source_wallet_agg.setdefault(
                src,
                {
                    "mints": set(),
                    "total": 0,
                    "conf_scores": [],
                },
            )
            agg["mints"].add(mint)
            agg["total"] += amt
            agg["conf_scores"].append(prov_conf)

    rpc.close()
    http_client.close()

    # Build source-wallet summaries.
    conf_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    src_summaries: List[SourceWalletSummary] = []
    for src, info in source_wallet_agg.items():
        mints_contrib = sorted(info["mints"])
        total_raw = info["total"]
        scores = [_short_conf_label(c) for c in info["conf_scores"]]
        if scores:
            highest = max(scores, key=lambda c: conf_rank.get(c, 0))
            lowest = min(scores, key=lambda c: conf_rank.get(c, 0))
            avg_score = sum(conf_rank.get(c, 0) for c in scores) / len(scores)
        else:
            highest = "LOW"
            lowest = "LOW"
            avg_score = 0.0
        src_summaries.append(
            SourceWalletSummary(
                source_wallet=src,
                mints_contributed=mints_contrib,
                total_transfers_raw=total_raw,
                highest_confidence=highest,
                lowest_confidence=lowest,
                overall_usefulness_score=avg_score,
            )
        )

    # Sort source summaries by usefulness score and volume.
    src_summaries_sorted = sorted(
        src_summaries,
        key=lambda s: (-s.overall_usefulness_score, -s.total_transfers_raw),
    )

    # Build final JSON.
    json_report = {
        "trading_wallet": TRADING_WALLET,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "recursion_depth": 1,
        "max_wallets_per_mint": MAX_WALLETS_PER_MINT,
        "mints": [r.to_dict() for r in per_mint_reports],
        "source_wallets": [s.to_dict() for s in src_summaries_sorted],
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_REPORT_PATH.write_text(json.dumps(json_report, indent=2), encoding="utf-8")

    # Markdown summary.
    lines: List[str] = []
    lines.append("# Full upstream provenance report")
    lines.append("")
    lines.append(f"- Trading wallet: `{TRADING_WALLET}`")
    lines.append(f"- Generated at: {json_report['generated_at']}")
    lines.append(
        f"- Mints analyzed: {len(per_mint_reports)}"
    )
    lines.append(
        f"- Unique source wallets (1-hop): {len(src_summaries_sorted)}"
    )
    lines.append("")

    # Per-mint puzzle view.
    lines.append("## Per-mint provenance puzzle")
    lines.append("")
    lines.append(
        "| Mint | Symbol | Balance_raw | Direct_proven | Upstream_proven | Inferred_only | Unresolved | Provenance_conf | Recommended_action |"
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for r in per_mint_reports:
        lines.append(
            f"| `{r.mint[:8]}…` | {r.symbol or ''} | {r.current_wallet_balance_raw} | "
            f"{r.direct_proven_raw} | {r.upstream_proven_raw} | {r.inferred_only_raw} | {r.unresolved_raw} | "
            f"{r.provenance_confidence} | {r.recommended_action} |"
        )
    lines.append("")

    # Source-wallet contribution table.
    if src_summaries_sorted:
        lines.append("## Source-wallet contributions")
        lines.append("")
        lines.append(
            "| Source wallet | Mints_contributed | Total_transfers_raw | Highest_conf | Lowest_conf | Usefulness_score |"
        )
        lines.append(
            "| --- | --- | --- | --- | --- | --- |"
        )
        for s in src_summaries_sorted:
            lines.append(
                f"| `{s.source_wallet[:8]}…` | {len(s.mints_contributed)} | {s.total_transfers_raw} | "
                f"{s.highest_confidence} | {s.lowest_confidence} | {s.overall_usefulness_score:.2f} |"
            )
        lines.append("")

    MD_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"JSON report written to {JSON_REPORT_PATH}")
    print(f"Markdown report written to {MD_REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

