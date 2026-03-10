from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .config import Config

JUPITER_TOKENS_V2_URL = "https://api.jup.ag/tokens/v2/search"


logger = logging.getLogger(__name__)


class JupiterError(RuntimeError):
    pass


def _retry_request(fn, max_retries: int) -> httpx.Response:
    backoff = 0.5
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = fn()
            resp.raise_for_status()
            return resp
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            logger.warning(
                "Jupiter request failed (attempt=%d/%d): %s",
                attempt,
                max_retries,
                exc,
            )
            if attempt >= max_retries:
                break
            time.sleep(backoff)
            backoff *= 2
    raise JupiterError(f"Jupiter request failed after {max_retries} attempts: {last_exc}")


def _jupiter_headers(config: Config) -> Dict[str, str]:
    """Headers for Jupiter API (x-api-key when JUPITER_API_KEY is set)."""
    headers: Dict[str, str] = {}
    api_key = getattr(config, "jupiter_api_key", None)
    if api_key and str(api_key).strip():
        headers["x-api-key"] = str(api_key).strip()
    return headers


def get_quote(
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    slippage_bps: int,
    config: Config,
) -> Dict[str, Any]:
    """
    Request a quote from Jupiter for a token swap.
    """

    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": str(slippage_bps),
        "swapMode": "ExactIn",
    }
    headers = _jupiter_headers(config)

    def _do():
        return httpx.get(
            config.jupiter_quote_url,
            params=params,
            headers=headers,
            timeout=config.rpc_timeout_s,
        )

    resp = _retry_request(_do, config.max_retries)
    return resp.json()


def get_quote_quick(
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    slippage_bps: int,
    config: Config,
    timeout_s: float = 8.0,
) -> Optional[Dict[str, Any]]:
    """
    Single-attempt quote for price probe only. Short timeout so the run loop does not block.
    Returns None on failure; use get_quote() for actual swap execution.
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": str(slippage_bps),
        "swapMode": "ExactIn",
    }
    headers = _jupiter_headers(config)
    try:
        resp = httpx.get(
            config.jupiter_quote_url,
            params=params,
            headers=headers,
            timeout=timeout_s,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def get_swap_tx(
    quote_response: Dict[str, Any],
    user_pubkey: str,
    config: Config,
) -> str:
    """
    Request a base64-encoded swap transaction from Jupiter for a previously obtained quote.
    """

    payload = {
        "userPublicKey": user_pubkey,
        "quoteResponse": quote_response,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": 0,
    }
    headers = _jupiter_headers(config)

    def _do():
        return httpx.post(
            config.jupiter_swap_url,
            json=payload,
            headers=headers,
            timeout=config.rpc_timeout_s,
        )

    resp = _retry_request(_do, config.max_retries)
    data = resp.json()
    tx = data.get("swapTransaction")
    if not isinstance(tx, str):
        raise JupiterError("Jupiter swap response missing swapTransaction")
    return tx


def get_token_metadata_batch(
    config: Config,
    mints: List[str],
) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """
    Fetch symbol and name for multiple mints from Jupiter Tokens API v2.
    Returns a dict mint -> (symbol, name). Mints not found or on error are omitted or have (None, None).
    Requires JUPITER_API_KEY (same as swap API). Skips request if no mints or no API key.
    """
    result: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    if not mints:
        return result
    api_key = getattr(config, "jupiter_api_key", None)
    if not api_key or not str(api_key).strip():
        logger.debug("Jupiter token metadata skipped: no JUPITER_API_KEY")
        return result

    query = ",".join(mints[:100])  # API limit 100
    headers = _jupiter_headers(config)
    try:
        resp = httpx.get(
            JUPITER_TOKENS_V2_URL,
            params={"query": query},
            headers=headers,
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("Jupiter token metadata request failed: %s", exc)
        return result

    if not isinstance(data, list):
        return result
    for item in data:
        if not isinstance(item, dict):
            continue
        mint_id = item.get("id")
        if not mint_id:
            continue
        symbol = item.get("symbol")
        name = item.get("name")
        result[mint_id] = (
            str(symbol).strip() if symbol else None,
            str(name).strip() if name else None,
        )
    return result

