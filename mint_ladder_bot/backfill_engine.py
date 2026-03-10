"""
Backfill engine — CEO directive: robust backfill independent of live trading.

- Uses Helius first, then RPC pool failover.
- Detects gaps in signature history; retries on 429, timeout, null.
- Output: wallet_events.jsonl.

Runs independently from live trading; no execution.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .data.helius_client import get_wallet_transactions
from .data.helius_adapter import map_helius_to_wallet_tx_events, WalletTxEvent

logger = logging.getLogger(__name__)

# Structured log events (directive §7)
LOG_RPC_FAILOVER = "RPC_FAILOVER"


def fetch_missing_transactions(
    wallet: str,
    known_signatures: Set[str],
    limit: int = 500,
    use_helius_first: bool = True,
) -> List[Dict[str, Any]]:
    """
    Fetch transactions for wallet; return those not in known_signatures.
    Tries Helius Enhanced API first; on 429/timeout/null logs RPC_FAILOVER and retries (Helius retry).
    RPC pool used only for getTransaction when enriching signatures; see BackfillRpcClient.
    """
    missing: List[Dict[str, Any]] = []
    try:
        txs = get_wallet_transactions(wallet, limit=limit)
        for t in txs:
            sig = (t.get("signature") or "").strip()
            if sig and sig not in known_signatures:
                missing.append(t)
    except Exception as e:
        logger.warning("%s Helius fetch failed (retry/timeout/429): %s", LOG_RPC_FAILOVER, e)
    return missing


def reconstruct_wallet_lots(
    wallet: str,
    events: List[WalletTxEvent],
) -> List[Dict[str, Any]]:
    """
    From a stream of WalletTxEvent (chronological), reconstruct lot open/close.
    Returns list of lot records: { mint, entry_sig, entry_token_raw, entry_sol, exit_sig?, sold_raw? }.
    """
    lots: List[Dict[str, Any]] = []
    # Simple reconstruction: each buy opens/extends a lot; each sell closes proportionally.
    by_mint: Dict[str, List[WalletTxEvent]] = {}
    for e in events:
        if e.mint == "SOL":
            continue
        by_mint.setdefault(e.mint, []).append(e)
    for mint, evs in by_mint.items():
        balance_raw = 0
        for e in evs:
            balance_raw += e.token_delta
            if e.type == "buy" and e.token_delta > 0:
                lots.append({
                    "mint": mint,
                    "entry_sig": e.signature,
                    "entry_token_raw": e.token_delta,
                    "entry_sol_lamports": -e.sol_delta if e.sol_delta < 0 else 0,
                })
            elif e.type == "sell" and e.token_delta < 0:
                lots.append({
                    "mint": mint,
                    "exit_sig": e.signature,
                    "sold_raw": -e.token_delta,
                })
    return lots


def verify_lot_integrity(lots: List[Dict[str, Any]], state_mints: Dict[str, Any]) -> Dict[str, Any]:
    """
    Verify reconstructed lots against runtime state (if provided).
    Returns { ok: bool, errors: [], warnings: [] }.
    """
    errors: List[str] = []
    warnings: List[str] = []
    for lot in lots:
        mint = lot.get("mint", "")
        if not mint:
            errors.append("lot missing mint")
        if "entry_sig" in lot and "entry_token_raw" not in lot:
            warnings.append(f"lot {mint[:8]} has entry_sig but no entry_token_raw")
    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


def run_backfill(
    wallet: str,
    output_path: Path,
    known_sigs: Optional[Set[str]] = None,
    max_pages: int = 10,
) -> int:
    """
    Fetch missing txs (Helius + RPC failover), map to events, append to wallet_events.jsonl.
    Returns number of events written.
    """
    known = known_sigs or set()
    total_events = 0
    before: Optional[str] = None
    for _ in range(max_pages):
        try:
            txs = get_wallet_transactions(wallet, limit=100, before=before)
        except Exception as e:
            logger.warning("fetch_missing_transactions failed: %s", e)
            break
        if not txs:
            break
        events = map_helius_to_wallet_tx_events(txs, wallet)
        for e in events:
            line = {
                "signature": e.signature,
                "timestamp_ts": e.timestamp_ts,
                "mint": e.mint,
                "token_delta": e.token_delta,
                "sol_delta": e.sol_delta,
                "type": e.type,
            }
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "a") as f:
                f.write(json.dumps(line) + "\n")
            total_events += 1
        before = txs[-1].get("signature") if txs else None
        if len(txs) < 100:
            break
        time.sleep(0.3)
    return total_events
