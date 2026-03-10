"""
CLI for trusted-transfer-scratch-report (Step 3 scratch reconstruction).

Read-only: no state mutation, no persistent lots, no mint resume.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from .config import Config
from .models import StatusFile
from .rpc import RpcClient
from .runtime_paths import get_state_path, get_status_path
from .state import load_state
from .transfer_provenance_analysis import run_transfer_provenance_analysis
from .transfer_provenance_scratch import (
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
    source_wallet: Optional[str] = None,
    mint: Optional[str] = None,
    max_signatures: Optional[int] = None,
    json_output: bool = False,
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

    # Get candidates from Step 2 (bounded)
    candidates = run_transfer_provenance_analysis(
        wallet=destination_wallet,
        mints_tracked=mints_tracked,
        rpc=rpc,
        max_signatures=min(max_sigs, 100),
        trusted_source_wallets=trusted,
        decimals_by_mint=decimals_by_mint,
        symbol_by_mint=symbol_by_mint,
        mint_filter={mint} if mint else None,
    )

    # If explicit source_wallet + mint, run single-candidate scratch (or synthesize one)
    if source_wallet and mint:
        # Find matching candidate or create synthetic for scratch
        match = next(
            (
                c
                for c in candidates
                if getattr(c, "mint", None) == mint
                and getattr(c, "source_wallet", None) == source_wallet
                and getattr(c, "classification", None) == "trusted-transfer-candidate"
            ),
            None,
        )
        if match:
            reports = [
                run_scratch_trusted_transfer_analysis(
                    destination_wallet=destination_wallet,
                    destination_state=state,
                    status=status,
                    mint=mint,
                    source_wallet=source_wallet,
                    tx_signature=getattr(match, "tx_signature", ""),
                    transferred_amount_raw=getattr(match, "amount_raw", 0),
                    trusted_source_wallets=trusted,
                    rpc=rpc,
                    max_signatures=max_sigs,
                    decimals_by_mint=decimals_by_mint,
                    symbol_by_mint=symbol_by_mint,
                )
            ]
        else:
            # No candidate from Step 2; run scratch anyway with synthetic (e.g. for testing)
            reports = [
                run_scratch_trusted_transfer_analysis(
                    destination_wallet=destination_wallet,
                    destination_state=state,
                    status=status,
                    mint=mint,
                    source_wallet=source_wallet,
                    tx_signature="synthetic",
                    transferred_amount_raw=1,
                    trusted_source_wallets=trusted,
                    rpc=rpc,
                    max_signatures=max_sigs,
                    decimals_by_mint=decimals_by_mint,
                    symbol_by_mint=symbol_by_mint,
                )
            ]
    else:
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

    if json_output:
        out = [r.to_dict() for r in reports]
        print(json.dumps(out, indent=2))
        return 0

    if not reports:
        print("No trusted transfer candidates to analyze (run transfer-provenance-report first or set TRUSTED_SOURCE_WALLETS).")
        return 0

    for r in reports:
        print("--- Scratch report (read-only) ---")
        print("destination_wallet:", r.destination_wallet[:16] + "…")
        print("source_wallet:", r.source_wallet[:16] + "…")
        print("mint:", r.mint[:16] + "…  symbol:", r.symbol)
        print("tx_signature:", r.tx_signature[:20] + "…")
        print("transferred_amount_raw:", r.transferred_amount_raw)
        print("source_reconstruction_status:", r.source_reconstruction_status)
        print("source_provenance_valid_available_raw:", r.source_provenance_valid_available_raw)
        print("amount_propagated_raw:", r.amount_propagated_raw)
        print("proposed_classification:", r.proposed_classification)
        print("proposed_entry_price_sol_per_token:", r.proposed_entry_price_sol_per_token)
        print("why:", r.why)
        print("BEFORE:", r.before)
        print("AFTER:", r.after)
        print("improvement_meaningful:", r.improvement_meaningful)
        print("improvement_numerical_only:", r.improvement_numerical_only)
        print("satisfies_strict_safety_rule:", r.satisfies_strict_safety_rule)
        print()
    return 0


def entry() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        description="Step 3: Scratch reconstruction for one trusted source wallet (no state mutation)."
    )
    ap.add_argument("--state", type=Path, default=None)
    ap.add_argument("--status", type=Path, default=None)
    ap.add_argument("--source-wallet", type=str, default=None, help="Trusted source wallet")
    ap.add_argument("--mint", type=str, default=None, help="Mint to analyze")
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
            source_wallet=args.source_wallet,
            mint=args.mint,
            max_signatures=args.max_signatures,
            json_output=args.json_output,
        )
    )
