"""
Helius client — CEO directive: Helius-first data architecture.

Single entry point for:
- Wallet transaction discovery (Helius Enhanced Transactions API)
- Token metadata (Helius DAS API)
- Signatures (Helius or RPC)
- Optional streaming of wallet activity

RPC defaults to Helius; backfill pool used only for failover.
No execution. Never log API keys or secrets.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterator, List, Optional

import httpx

from .helius_adapter import (
    WalletTxEvent,
    fetch_enhanced_transactions,
    get_wallet_tx_events,
    map_helius_to_wallet_tx_events,
)

logger = logging.getLogger(__name__)


def _helius_base() -> str:
    return (os.getenv("HELIUS_API_URL") or "").strip().rstrip("/") or "https://api-mainnet.helius-rpc.com"


def _api_key() -> str:
    return (os.getenv("HELIUS_API_KEY") or "").strip()


class HeliusClientError(RuntimeError):
    pass


def get_wallet_transactions(wallet: str, limit: int = 100, before: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch wallet transactions from Helius Enhanced Transactions API. Primary discovery path."""
    key = _api_key()
    if not key:
        raise HeliusClientError("HELIUS_API_KEY not set")
    base = _helius_base()
    url = f"{base}/v0/addresses/{wallet}/transactions"
    params: Dict[str, Any] = {
        "api-key": key,
        "limit": min(max(1, limit), 100),
        "token-accounts": "balanceChanged",
        "sort-order": "desc",
    }
    if before:
        params["before-signature"] = before
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            logger.warning("Helius 429 rate limit; consider backoff")
        raise HeliusClientError(f"Helius transactions failed: {e}") from e
    except Exception as e:
        raise HeliusClientError(f"Helius transactions failed: {e}") from e


def get_token_metadata(mint: str) -> Optional[Dict[str, Any]]:
    """Fetch token metadata from Helius DAS API (or RPC getAsset). Returns name, symbol, decimals when available."""
    key = _api_key()
    if not key:
        return None
    base = _helius_base().replace("/v0", "")  # DAS often on same host, different path
    # Helius DAS: POST with getAsset
    url = f"{base}/?api-key={key}" if "?" not in base else f"{base}&api-key={key}"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAsset",
        "params": {"id": mint},
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result") if isinstance(data, dict) else None
            if result and isinstance(result, dict):
                return {
                    "name": result.get("content", {}).get("metadata", {}).get("name"),
                    "symbol": result.get("content", {}).get("metadata", {}).get("symbol"),
                    "decimals": result.get("content", {}).get("metadata", {}).get("decimals"),
                }
            return None
    except Exception as e:
        logger.debug("get_token_metadata %s failed: %s", mint[:8], e)
        return None


def get_signatures(address: str, limit: int = 100, before: Optional[str] = None) -> List[str]:
    """Get transaction signatures for address. Uses Helius Enhanced Transactions when available."""
    txs = get_wallet_transactions(address, limit=limit, before=before)
    return [t.get("signature") or "" for t in txs if t.get("signature")]


def stream_wallet_activity(
    wallet: str,
    poll_interval_sec: float = 30.0,
    limit_per_fetch: int = 50,
) -> Iterator[List[WalletTxEvent]]:
    """Yield batches of WalletTxEvent by polling Helius. For high-scale use, prefer websocket when available."""
    last_sig: Optional[str] = None
    while True:
        try:
            txs = get_wallet_transactions(wallet, limit=limit_per_fetch, before=last_sig)
            if not txs:
                yield []
            else:
                events = map_helius_to_wallet_tx_events(txs, wallet)
                if txs:
                    last_sig = txs[-1].get("signature")
                yield events
        except HeliusClientError as e:
            logger.warning("stream_wallet_activity error: %s", e)
            yield []
        time.sleep(poll_interval_sec)
