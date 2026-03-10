"""
Helius Adapter Layer — CEO directive: Helius as canonical data provider.

Internal schema WalletTxEvent abstracts vendor lock-in. This module:
- Calls Helius Enhanced Transactions API
- Maps response → WalletTxEvent
- Returns ordered event stream for ledger engine consumption.

No execution logic. Validation only until Phase 2 is authorized.
"""
from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Helius Enhanced Transactions API (mainnet). Override via HELIUS_API_URL env.
def _helius_base() -> str:
    return (os.getenv("HELIUS_API_URL") or "").strip().rstrip("/") or "https://api-mainnet.helius-rpc.com"


@dataclass
class WalletTxEvent:
    """
    Internal schema: one event per (signature, mint) with token and SOL deltas.
    Abstracts vendor lock-in; future providers map to this same schema.
    """
    signature: str
    timestamp: Optional[datetime]  # block time (use timestamp_ts for unix int)
    mint: str
    token_delta: int  # raw units; positive = received, negative = sent
    sol_delta: int    # lamports; positive = SOL in, negative = SOL out (includes fee for net)
    type: str         # "buy" | "sell" | "transfer"

    @property
    def timestamp_ts(self) -> Optional[int]:
        """Unix timestamp (int) for schema compatibility."""
        if self.timestamp is None:
            return None
        return int(self.timestamp.timestamp())


def _sol_delta_lamports_for_wallet(
    native_transfers: List[Dict[str, Any]],
    wallet: str,
) -> int:
    """Sum SOL delta for wallet from nativeTransfers. Negative = spent."""
    total = 0
    for nt in native_transfers or []:
        from_acc = nt.get("fromUserAccount") or ""
        to_acc = nt.get("toUserAccount") or ""
        amount = int(nt.get("amount") or 0)
        if from_acc == wallet:
            total -= amount
        if to_acc == wallet:
            total += amount
    return total


def _token_deltas_for_wallet(
    token_transfers: List[Dict[str, Any]],
    wallet: str,
) -> Dict[str, int]:
    """Per-mint token delta (raw) for wallet. Positive = received."""
    deltas: Dict[str, int] = {}
    for tt in token_transfers or []:
        from_acc = tt.get("fromUserAccount") or ""
        to_acc = tt.get("toUserAccount") or ""
        mint = tt.get("mint")
        if not mint:
            continue
        # tokenAmount may be UI amount; prefer rawTokenAmount.tokenAmount (string) if present
        raw = tt.get("rawTokenAmount") or {}
        if isinstance(raw, dict) and raw.get("tokenAmount") is not None:
            try:
                amt = int(raw["tokenAmount"])
            except (ValueError, TypeError):
                amt = int(float(tt.get("tokenAmount") or 0) * (10 ** int(raw.get("decimals") or 6)))
        else:
            # Fallback: tokenAmount as number, assume 6 decimals if missing
            dec = int(tt.get("decimals") or 6)
            amt = int(float(tt.get("tokenAmount") or 0) * (10 ** dec))
        if from_acc == wallet:
            deltas[mint] = deltas.get(mint, 0) - amt
        if to_acc == wallet:
            deltas[mint] = deltas.get(mint, 0) + amt
    return deltas


def _classify_type(token_delta: int, sol_delta: int) -> str:
    """Classify as buy (token in, SOL out), sell (token out, SOL in), or transfer."""
    if token_delta > 0 and sol_delta < 0:
        return "buy"
    if token_delta < 0 and sol_delta > 0:
        return "sell"
    return "transfer"


def fetch_enhanced_transactions(
    address: str,
    api_key: str,
    limit: int = 50,
    before_signature: Optional[str] = None,
    token_accounts: str = "balanceChanged",
    api_base: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Call Helius Enhanced Transactions API. Returns list of enhanced tx objects.
    """
    base = (api_base or _helius_base()).rstrip("/")
    url = f"{base}/v0/addresses/{address}/transactions"
    params: Dict[str, Any] = {
        "api-key": api_key,
        "limit": min(max(1, limit), 100),
        "token-accounts": token_accounts,
        "sort-order": "desc",
    }
    if before_signature:
        params["before-signature"] = before_signature
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


def map_helius_to_wallet_tx_events(
    enhanced_txs: List[Dict[str, Any]],
    wallet: str,
) -> List[WalletTxEvent]:
    """
    Map Helius enhanced transaction list to ordered WalletTxEvent stream.
    One event per (signature, mint) with non-zero token_delta.
    """
    events: List[WalletTxEvent] = []
    for tx in enhanced_txs or []:
        sig = tx.get("signature") or ""
        ts = tx.get("timestamp")
        if ts is not None:
            try:
                timestamp = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                timestamp = None
        else:
            timestamp = None
        fee = int(tx.get("fee") or 0)
        native = tx.get("nativeTransfers") or []
        token = tx.get("tokenTransfers") or []
        sol_delta = _sol_delta_lamports_for_wallet(native, wallet)
        # Optionally subtract fee from net SOL out (spent) so sol_delta = net
        if sol_delta < 0 and fee > 0:
            sol_delta -= fee
        token_deltas = _token_deltas_for_wallet(token, wallet)
        for mint, delta in token_deltas.items():
            if delta == 0:
                continue
            event_type = _classify_type(delta, sol_delta)
            events.append(
                WalletTxEvent(
                    signature=sig,
                    timestamp=timestamp,
                    mint=mint,
                    token_delta=delta,
                    sol_delta=sol_delta,
                    type=event_type,
                )
            )
        # Pure SOL transfer (no token movement)
        if not token_deltas and sol_delta != 0:
            events.append(
                WalletTxEvent(
                    signature=sig,
                    timestamp=timestamp,
                    mint="SOL",
                    token_delta=0,
                    sol_delta=sol_delta,
                    type="transfer",
                )
            )
    return events


def get_wallet_tx_events(
    address: str,
    api_key: str,
    limit: int = 50,
    before_signature: Optional[str] = None,
) -> List[WalletTxEvent]:
    """
    Fetch last `limit` enhanced transactions for address and return
    ordered WalletTxEvent stream. Single page; pagination via before_signature.
    """
    raw = fetch_enhanced_transactions(
        address=address,
        api_key=api_key,
        limit=limit,
        before_signature=before_signature,
    )
    return map_helius_to_wallet_tx_events(raw, address)


def _one_tx_to_events(tx: Dict[str, Any], wallet: str) -> List[WalletTxEvent]:
    """Map a single Helius enhanced tx to WalletTxEvent list (for map_to_wallet_events)."""
    return map_helius_to_wallet_tx_events([tx], wallet)


class HeliusAdapter:
    """
    Helius data adapter — CEO directive Phase 1.
    Wraps Helius Enhanced Transactions API and maps to internal WalletTxEvent schema.
    No execution logic. Do not integrate with runner until Phase 2 is authorized.
    """

    def __init__(self, api_key: str, api_base: Optional[str] = None) -> None:
        self._api_key = api_key
        self._api_base = api_base or _helius_base()

    def get_wallet_transactions(self, wallet: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Fetch last `limit` enhanced transactions for wallet. Read-only."""
        return fetch_enhanced_transactions(
            address=wallet,
            api_key=self._api_key,
            limit=min(limit, 100),
            api_base=self._api_base,
        )

    def map_to_wallet_events(self, helius_tx: Dict[str, Any], wallet: str) -> List[WalletTxEvent]:
        """Map one Helius enhanced tx to normalized WalletTxEvent list."""
        return _one_tx_to_events(helius_tx, wallet)
