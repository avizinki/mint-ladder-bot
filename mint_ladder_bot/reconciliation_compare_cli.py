"""
Step 4: Before/after reconciliation comparison for transfer-provenance candidates.

Read-only: runs Step 2 candidates + Step 3 scratch propagation, outputs
structured comparison (identity, before, after, transfer result, improvement assessment).
Does NOT mutate state, integrate with launch, or change trading.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config
from .models import StatusFile
from .rpc import RpcClient
from .runtime_paths import get_state_path, get_status_path
from .state import load_state
from .transfer_provenance_analysis import run_transfer_provenance_analysis
from .transfer_provenance_scratch import (
    build_reconciliation_comparison_report,
    run_scratch_report_for_candidates,
    run_scratch_trusted_transfer_analysis,
)


def _load_status(status_path: Path) -> Optional[StatusFile]:
    if not status_path.exists():
        return None
    try:
        return StatusFile.model_validate_json(status_path.read_text())
    except Exception:
        return None


def _decimals_and_symbols_from_status(status: StatusFile) -> tuple[dict, dict]:
    dec: dict = {}
    sym: dict = {}
    for m in status.mints:
        dec[m.mint] = getattr(m, "decimals", 6)
        if getattr(m, "symbol", None):
            sym[m.mint] = m.symbol
    return dec, sym


def main(
    state_path: Optional[Path] = None,
    status_path: Optional[Path] = None,
    source_wallet_filter: Optional[str] = None,
    mint_filter: Optional[str] = None,
    max_signatures: Optional[int] = None,
    json_output: bool = False,
    synthetic_candidates: Optional[List[Dict[str, Any]]] = None,
) -> int:
    state_path = state_path or get_state_path()
    status_path = status_path or get_status_path()

    if not state_path.exists() or not status_path.exists():
        print("state.json or status.json not found.", file=sys.stderr)
        return 1

    status = _load_status(status_path)
    if status is None:
        print("Failed to load status.", file=sys.stderr)
        return 1

    state = load_state(state_path, status_path)
    destination_wallet = getattr(state, "wallet", None) or getattr(status, "wallet", None)
    if not destination_wallet:
        print("No destination wallet in state/status.", file=sys.stderr)
        return 1

    decimals_by_mint, symbol_by_mint = _decimals_and_symbols_from_status(status)
    mints_tracked = set(state.mints.keys()) or {m.mint for m in status.mints}
    config = Config()
    trusted = getattr(config, "trusted_source_wallets", []) or []
    max_sigs = max_signatures or getattr(
        config, "reconstruction_max_signatures_per_wallet", 500
    )
    rpc = RpcClient(config.rpc_endpoint, timeout_s=config.rpc_timeout_s)

    if synthetic_candidates:
        from types import SimpleNamespace
        candidates = [SimpleNamespace(**c) for c in synthetic_candidates]
        comparison_label = "synthetic"
    else:
        candidates = run_transfer_provenance_analysis(
            wallet=destination_wallet,
            mints_tracked=mints_tracked,
            rpc=rpc,
            max_signatures=min(max_sigs, 100),
            trusted_source_wallets=trusted,
            decimals_by_mint=decimals_by_mint,
            symbol_by_mint=symbol_by_mint,
            mint_filter={mint_filter} if mint_filter else None,
        )
        if source_wallet_filter:
            candidates = [c for c in candidates if getattr(c, "source_wallet", None) == source_wallet_filter]
        comparison_label = "real"

    reports = run_scratch_report_for_candidates(
        destination_wallet=destination_wallet,
        destination_state=state,
        status=status,
        candidates=candidates,
        trusted_source_wallets=trusted,
        rpc=rpc,
        max_signatures=max_sigs,
        decimals_by_mint=decimals_by_mint,
        symbol_by_mint=symbol_by_mint,
    )

    comparisons: List[Dict[str, Any]] = []
    for r in reports:
        comparisons.append(build_reconciliation_comparison_report(r))

    if not comparisons and not json_output:
        print("No trusted transfer candidates to compare (run transfer-provenance-report and set TRUSTED_SOURCE_WALLETS).")
        return 0

    if json_output:
        out = {
            "comparison_label": comparison_label,
            "candidates_analyzed": len(comparisons),
            "comparisons": comparisons,
        }
        print(json.dumps(out, indent=2))
        return 0

    print("Reconciliation comparison (Step 4 — read-only)")
    print("Label:", comparison_label)
    print("Candidates analyzed:", len(comparisons))
    print()
    for i, comp in enumerate(comparisons):
        print("--- Candidate", i + 1, "---")
        print("IDENTITY:", comp["identity"])
        print("BEFORE:", comp["before"])
        print("AFTER:", comp["after"])
        print("TRANSFER_RESULT:", comp["transfer_result"])
        print("IMPROVEMENT_ASSESSMENT:", comp["improvement_assessment"])
        print()
    return 0


def entry() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        description="Step 4: Before/after reconciliation comparison for transfer-provenance candidates (read-only)."
    )
    ap.add_argument("--state", type=Path, default=None)
    ap.add_argument("--status", type=Path, default=None)
    ap.add_argument("--source-wallet", type=str, default=None, help="Filter to this source wallet")
    ap.add_argument("--mint", type=str, default=None, help="Filter to this mint")
    ap.add_argument("--max-signatures", type=int, default=None)
    ap.add_argument("--json", action="store_true", dest="json_output")
    args = ap.parse_args()
    sp = args.state
    stp = args.status
    if sp is not None and not sp.is_absolute():
        sp = Path.cwd() / sp
    if stp is not None and not stp.is_absolute():
        stp = Path.cwd() / stp
    sys.exit(
        main(
            state_path=sp,
            status_path=stp,
            source_wallet_filter=args.source_wallet,
            mint_filter=args.mint,
            max_signatures=args.max_signatures,
            json_output=args.json_output,
        )
    )
