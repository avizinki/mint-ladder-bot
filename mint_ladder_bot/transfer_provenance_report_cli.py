"""
CLI for transfer-provenance-report (Step 2 read-only analysis).

Usage:
  transfer-provenance-report [options]
  python -m mint_ladder_bot.transfer_provenance_report_cli [options]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from .config import Config
from .rpc import RpcClient
from .runtime_paths import get_state_path, get_status_path
from .state import load_state
from .transfer_provenance_analysis import run_transfer_provenance_analysis


def _load_wallet_and_mints(
    state_path: Path,
    status_path: Path,
    wallet_override: Optional[str],
) -> tuple[str, set[str], dict, dict]:
    """Return (wallet, mints_set, decimals_by_mint, symbol_by_mint)."""
    wallet = (wallet_override or "").strip()
    decimals_by_mint: dict = {}
    symbol_by_mint: dict = {}
    mints: set = set()

    if status_path.exists():
        try:
            data = json.loads(status_path.read_text())
            if not wallet:
                wallet = (data.get("wallet") or "").strip()
            for m in data.get("mints") or []:
                if not isinstance(m, dict) or not m.get("mint"):
                    continue
                mint = m["mint"]
                mints.add(mint)
                decimals_by_mint[mint] = int(m.get("decimals", 6))
                if m.get("symbol"):
                    symbol_by_mint[mint] = m["symbol"]
        except Exception:
            pass

    if not wallet and state_path.exists():
        try:
            state = load_state(state_path, status_path)
            wallet = getattr(state, "wallet", None) or ""
            mints = set(getattr(state, "mints", {}).keys())
            for mint in getattr(state, "mints", {}):
                decimals_by_mint.setdefault(mint, 6)
        except Exception:
            pass

    return wallet, mints, decimals_by_mint, symbol_by_mint


def main(
    state_path: Optional[Path] = None,
    status_path: Optional[Path] = None,
    wallet_override: Optional[str] = None,
    max_signatures: Optional[int] = None,
    mint_filter: Optional[list[str]] = None,
    json_output: bool = False,
) -> int:
    state_path = state_path or get_state_path()
    status_path = status_path or get_status_path()

    wallet, mints_set, decimals_by_mint, symbol_by_mint = _load_wallet_and_mints(
        state_path, status_path, wallet_override
    )
    if not wallet:
        print("No wallet. Set --wallet or provide status/state with wallet.", file=sys.stderr)
        return 1
    if not mints_set:
        print("No mints. Provide status.json or state.json with mints.", file=sys.stderr)
        return 1

    mf = set(mint_filter) if mint_filter else None
    config = Config()
    max_sigs = max_signatures or getattr(
        config, "reconstruction_max_signatures_per_wallet", 500
    )
    trusted = getattr(config, "trusted_source_wallets", []) or []

    rpc = RpcClient(config.rpc_endpoint, timeout_s=config.rpc_timeout_s)
    candidates = run_transfer_provenance_analysis(
        wallet=wallet,
        mints_tracked=mints_set,
        rpc=rpc,
        max_signatures=max_sigs,
        trusted_source_wallets=trusted,
        decimals_by_mint=decimals_by_mint,
        symbol_by_mint=symbol_by_mint,
        mint_filter=mf,
    )

    if json_output:
        out = [c.to_dict() for c in candidates]
        print(json.dumps(out, indent=2))
        return 0

    print("Transfer-provenance report (read-only)")
    print("Wallet:", wallet[:16] + "…" if len(wallet) > 16 else wallet)
    print("Trusted source wallets:", len(trusted))
    print("Candidates:", len(candidates))
    print()
    for c in candidates:
        sym = c.symbol or c.mint[:8]
        src = (c.source_wallet[:12] + "…") if c.source_wallet and len(c.source_wallet) > 12 else (c.source_wallet or "—")
        print(f"  {sym}  {c.tx_signature[:16]}…  amount_raw={c.amount_raw}  source={src}")
        print(f"    classification={c.classification}  reason={c.reason}")
        print(f"    source_in_trusted_list={c.source_in_trusted_list}  could_help_reconciliation={c.could_help_reconciliation}")
        print()
    return 0


def entry() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        description="Read-only transfer-provenance report (Step 2; no state mutation)."
    )
    ap.add_argument("--state", type=Path, default=None, help="state.json path")
    ap.add_argument("--status", type=Path, default=None, help="status.json path")
    ap.add_argument("--wallet", type=str, default=None, help="Wallet pubkey override")
    ap.add_argument("--max-signatures", type=int, default=None, help="Max tx signatures to scan")
    ap.add_argument("--mint", action="append", dest="mints", default=[], help="Filter to these mints")
    ap.add_argument("--json", action="store_true", dest="json_output", help="Output JSON only")
    args = ap.parse_args()
    state_path = args.state
    status_path = args.status
    if state_path is not None and not state_path.is_absolute():
        state_path = Path.cwd() / state_path
    if status_path is not None and not status_path.is_absolute():
        status_path = Path.cwd() / status_path
    sys.exit(
        main(
            state_path=state_path,
            status_path=status_path,
            wallet_override=args.wallet,
            max_signatures=args.max_signatures,
            mint_filter=args.mints or None,
            json_output=args.json_output,
        )
    )
