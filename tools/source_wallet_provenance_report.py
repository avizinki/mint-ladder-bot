#!/usr/bin/env python3
"""
Source-wallet transfer provenance report (read-only).

For a fixed source wallet and the trading wallet, this tool:

1. Scans the trading wallet history for transfer-in events where the given
   source wallet sent tokens to the trading wallet (via transfer, not swap).
2. For each such transfer, reconstructs the source wallet history in scratch
   for that mint and attributes the transferred amount to provenance-valid lots.
3. When attribution succeeds, derives an implied entry price (SOL per token).
4. When attribution fails, falls back to a pool price estimate via Dexscreener.
5. Produces machine-readable JSON and human-readable markdown reports.

Constraints:
- No mutation of runtime state (state.json) or status.json.
- All reconstruction is scratch-only; lots are never persisted.
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

# Trading wallet (destination) and source wallet from CEO directive.
TRADING_WALLET = os.environ.get(
    "TRADING_WALLET",
    "3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR",
)
SOURCE_WALLET = os.environ.get(
    "SOURCE_WALLET",
    "9T6wvKnUiQDctcE8DyN8kfMxQcchqJzFQNiXfvYvU1fY",
)

DATA_DIR = _REPO / "runtime" / "projects" / "mint_ladder_bot"
STATE_PATH = DATA_DIR / "state.json"
STATUS_PATH = DATA_DIR / "status.json"
JSON_REPORT_PATH = DATA_DIR / "source_wallet_provenance_report.json"
MD_REPORT_PATH = DATA_DIR / "source_wallet_provenance_report.md"


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
        # Pick the pair with highest liquidity (best proxy for main pool).
        best = None
        best_liq = -1.0
        for p in pairs:
            liq = p.get("liquidity", {}).get("usd")
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
class ProvenanceRow:
    mint: str
    symbol: Optional[str]
    transfer_sig: str
    transfer_slot: Optional[int]
    transfer_time: Optional[str]
    amount_transferred_raw: int
    source_swap_sig: Optional[str]
    dex: Optional[str]
    sol_spent: Optional[float]
    tokens_received_raw: Optional[int]
    entry_price_estimated: Optional[float]
    confidence_score: str  # HIGH_CONFIDENCE | MEDIUM_CONFIDENCE | LOW_CONFIDENCE
    method: str  # e.g. fifo_source_lots | dexscreener_latest | none

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _fifo_with_lots(
    lots_ordered: List[Any],
    amount_raw: int,
    decimals: int,
) -> Tuple[int, float, bool, List[Any]]:
    """
    FIFO attribution with lot list tracking.

    Returns (attributed_amount_raw, cost_sol, success, lots_used).
    """
    if amount_raw <= 0:
        return 0, 0.0, True, []
    remaining_to_cover = amount_raw
    cost_sol = 0.0
    lots_used: List[Any] = []
    for lot in lots_ordered:
        if remaining_to_cover <= 0:
            break
        try:
            rem = int(getattr(lot, "remaining_amount", 0) or 0)
        except (ValueError, TypeError):
            rem = 0
        if rem <= 0:
            continue
        ep = getattr(lot, "entry_price_sol_per_token", None)
        if ep is None or ep <= 0 or ep < 1e-12 or ep > 1e3:
            return 0, 0.0, False, []
        take = min(rem, remaining_to_cover)
        token_human = take / (10 ** decimals)
        cost_sol += token_human * ep
        remaining_to_cover -= take
        lots_used.append(lot)
    if remaining_to_cover > 0:
        return amount_raw - remaining_to_cover, cost_sol, False, lots_used
    return amount_raw, cost_sol, True, lots_used


def main() -> int:
    from httpx import Client

    from mint_ladder_bot.config import Config
    from mint_ladder_bot.models import StatusFile
    from mint_ladder_bot.rpc import RpcClient
    from mint_ladder_bot.runtime_paths import get_state_path, get_status_path
    from mint_ladder_bot.state import load_state
    from mint_ladder_bot.transfer_provenance_analysis import (
        CLASS_TRUSTED_TRANSFER_CANDIDATE,
        run_transfer_provenance_analysis,
    )
    from mint_ladder_bot.transfer_provenance_scratch import (
        _provenance_valid_lots_ordered,
        run_source_wallet_scratch_reconstruction,
    )

    config = Config()
    state_path = get_state_path()
    status_path = get_status_path()

    if not status_path.exists():
        print("status.json not found; run status snapshot first.", file=sys.stderr)
        return 1

    status = StatusFile.model_validate_json(status_path.read_text())
    trading_wallet = status.wallet
    if TRADING_WALLET and TRADING_WALLET != trading_wallet:
        print(
            f"Warning: TRADING_WALLET={TRADING_WALLET} differs from status wallet={trading_wallet}",
            file=sys.stderr,
        )

    # Determine mints, decimals, and symbols from status.
    mints_set = {m.mint for m in status.mints}
    decimals_by_mint: Dict[str, int] = {
        m.mint: getattr(m, "decimals", 6) for m in status.mints
    }
    symbol_by_mint: Dict[str, str] = {
        m.mint: (m.symbol or m.mint[:8]) for m in status.mints
    }

    # Load state (read-only) for completeness; not modified.
    if state_path.exists():
        state = load_state(state_path, status_path)
    else:
        state = None

    # RPC client for transaction history and source scratch.
    rpc = RpcClient(config.rpc_endpoint, timeout_s=config.rpc_timeout_s)
    max_sigs = getattr(config, "reconstruction_max_signatures_per_wallet", 500)

    # Step 1: find transfer-in candidates for trading wallet where source_wallet matches SOURCE_WALLET.
    candidates = run_transfer_provenance_analysis(
        wallet=trading_wallet,
        mints_tracked=mints_set,
        rpc=rpc,
        max_signatures=max_sigs,
        trusted_source_wallets=[SOURCE_WALLET],
        decimals_by_mint=decimals_by_mint,
        symbol_by_mint=symbol_by_mint,
        mint_filter=None,
    )

    # Filter to trusted-transfer-candidate from the specified source wallet.
    relevant = [
        c
        for c in candidates
        if getattr(c, "classification", None) == CLASS_TRUSTED_TRANSFER_CANDIDATE
        and getattr(c, "source_wallet", None) == SOURCE_WALLET
    ]

    http_client = Client()

    rows: List[ProvenanceRow] = []

    for c in relevant:
        mint = getattr(c, "mint", None)
        if not mint:
            continue
        symbol = symbol_by_mint.get(mint)
        tx_sig = getattr(c, "tx_signature", "")
        slot = getattr(c, "slot", None)
        bt = getattr(c, "block_time", None)
        if isinstance(bt, datetime):
            transfer_time = bt.isoformat()
        else:
            transfer_time = None
        try:
            amt_raw = int(getattr(c, "amount_raw", 0) or 0)
        except (ValueError, TypeError):
            amt_raw = 0
        if amt_raw <= 0:
            continue

        decimals = decimals_by_mint.get(mint, 6)

        # Step 2: reconstruct source wallet in scratch and attempt FIFO attribution.
        scratch_state, recon_status = run_source_wallet_scratch_reconstruction(
            source_wallet=SOURCE_WALLET,
            mint=mint,
            rpc=rpc,
            max_signatures=max_sigs,
            decimals_by_mint=decimals_by_mint,
            symbol_by_mint=symbol_by_mint,
        )
        src_ms = scratch_state.mints.get(mint)
        lots_ordered = _provenance_valid_lots_ordered(src_ms) if src_ms else []

        tokens_received_raw: Optional[int] = None
        sol_spent: Optional[float] = None
        entry_price_est: Optional[float] = None
        source_swap_sig: Optional[str] = None
        dex_label: Optional[str] = None
        confidence = "LOW_CONFIDENCE"
        method = "none"

        if recon_status == "success" and lots_ordered:
            attributed_raw, cost_sol, ok, lots_used = _fifo_with_lots(
                [lot for lot, _rem in lots_ordered],
                amt_raw,
                decimals,
            )
            if ok and attributed_raw == amt_raw and attributed_raw > 0:
                tokens_received_raw = attributed_raw
                sol_spent = cost_sol
                token_human = attributed_raw / (10 ** decimals)
                if token_human > 0:
                    entry_price_est = cost_sol / token_human
                # Choose representative swap signature and dex/program from first used lot.
                rep_lot = lots_used[0] if lots_used else lots_ordered[0][0]
                source_swap_sig = getattr(rep_lot, "tx_signature", None)
                dex_label = getattr(rep_lot, "program_or_venue", None)
                confidence = "HIGH_CONFIDENCE"
                method = "fifo_source_lots"

        # Step 3: Dexscreener fallback when FIFO attribution did not yield price.
        if entry_price_est is None:
            price_native = _dexscreener_price_native(http_client, mint)
            if price_native is not None:
                tokens_received_raw = amt_raw
                token_human = amt_raw / (10 ** decimals)
                sol_spent = token_human * price_native
                entry_price_est = price_native
                confidence = "MEDIUM_CONFIDENCE"
                method = "dexscreener_latest"

        if entry_price_est is None:
            confidence = "LOW_CONFIDENCE"

        row = ProvenanceRow(
            mint=mint,
            symbol=symbol,
            transfer_sig=tx_sig,
            transfer_slot=slot,
            transfer_time=transfer_time,
            amount_transferred_raw=amt_raw,
            source_swap_sig=source_swap_sig,
            dex=dex_label,
            sol_spent=sol_spent,
            tokens_received_raw=tokens_received_raw,
            entry_price_estimated=entry_price_est,
            confidence_score=confidence,
            method=method,
        )
        rows.append(row)

    rpc.close()
    http_client.close()

    # Build JSON report.
    json_report = {
        "destination_wallet": trading_wallet,
        "source_wallet": SOURCE_WALLET,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_transfers_analyzed": len(rows),
        "rows": [r.to_dict() for r in rows],
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_REPORT_PATH.write_text(json.dumps(json_report, indent=2), encoding="utf-8")

    # Markdown report.
    lines: List[str] = []
    lines.append("# Source-wallet transfer provenance report")
    lines.append("")
    lines.append(f"- Destination (trading) wallet: `{trading_wallet}`")
    lines.append(f"- Source wallet: `{SOURCE_WALLET}`")
    lines.append(f"- Generated at: {json_report['generated_at']}")
    lines.append(f"- Transfers analyzed: {len(rows)}")
    lines.append("")
    if rows:
        lines.append("## Transfers")
        lines.append("")
        lines.append(
            "| Mint | Symbol | Transfer Sig | Amount (raw) | Source Swap Sig | Dex | Entry Price (SOL) | Confidence | Method |"
        )
        lines.append(
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
        )
        for r in rows:
            price_str = (
                f"{r.entry_price_estimated}"
                if r.entry_price_estimated is not None
                else ""
            )
            lines.append(
                f"| `{r.mint[:8]}…` | {r.symbol or ''} | `{r.transfer_sig[:12]}…` | {r.amount_transferred_raw} | "
                f"`{(r.source_swap_sig or '')[:12]}…` | {r.dex or ''} | "
                f"{price_str} | {r.confidence_score} | {r.method} |"
            )
        lines.append("")

    MD_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"JSON report written to {JSON_REPORT_PATH}")
    print(f"Markdown report written to {MD_REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

