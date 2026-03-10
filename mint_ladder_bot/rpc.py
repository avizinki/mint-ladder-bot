from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import httpx


logger = logging.getLogger(__name__)


def _sanitize_rpc_log(msg: str) -> str:
    """Redact API keys and secrets from RPC URLs before logging."""
    return re.sub(r"api-key=[^&\s'\"]+", "api-key=REDACTED", msg, flags=re.IGNORECASE)


class RpcError(RuntimeError):
    pass


class RpcClient:
    def __init__(self, endpoint: str, timeout_s: float = 20.0, max_retries: int = 5) -> None:
        self._endpoint = endpoint.strip().rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._client = httpx.Client(timeout=timeout_s)
        self._rpc_debug_logged = False

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, params: List[Any]) -> Any:
        if not self._rpc_debug_logged:
            parsed = urlparse(self._endpoint)
            qs = parse_qs(parsed.query)
            api_key_present = "api-key" in qs or "api_key" in qs
            logger.info(
                "RPC client endpoint (sanitized): host=%s api_key_param_present=%s",
                parsed.netloc or "unknown",
                api_key_present,
            )
            self._rpc_debug_logged = True
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        backoff = 0.5
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = self._client.post(self._endpoint, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data and data["error"]:
                    raise RpcError(f"RPC {method} error: {data['error']}")
                return data["result"]
            except (httpx.RequestError, httpx.HTTPStatusError, RpcError) as exc:
                last_exc = exc
                logger.warning(
                    "RPC request failed (method=%s, attempt=%d/%d): %s",
                    method,
                    attempt,
                    self._max_retries,
                    _sanitize_rpc_log(str(exc)),
                )
                if attempt >= self._max_retries:
                    break
                time.sleep(backoff)
                backoff *= 2
        raise RpcError(
            f"RPC {method} failed after {self._max_retries} attempts: {_sanitize_rpc_log(str(last_exc))}"
        )

    def validate(self) -> tuple[bool, float]:
        """
        Validate RPC endpoint is reachable. Returns (success, latency_ms).
        Uses getHealth then getLatestBlockhash; does not log endpoint URL.
        """
        start = time.monotonic()
        try:
            self._request("getHealth", [])
        except Exception:
            try:
                self._request("getLatestBlockhash", [{"commitment": "processed"}])
            except Exception:
                return False, (time.monotonic() - start) * 1000.0
        return True, (time.monotonic() - start) * 1000.0

    def measure_latency_ms(self) -> float:
        start = time.monotonic()
        try:
            self._request("getLatestBlockhash", [{"commitment": "processed"}])
        except Exception:
            try:
                self._request("getHealth", [])
            except Exception:
                pass
        end = time.monotonic()
        return (end - start) * 1000.0

    def get_balance(self, pubkey: str) -> int:
        result = self._request("getBalance", [pubkey, {"commitment": "confirmed"}])
        return int(result["value"])

    def get_slot(self) -> int:
        """Current slot (for duplicate-detection fingerprints)."""
        result = self._request("getSlot", [])
        return int(result)

    def get_token_account_balance(self, token_account_pubkey: str) -> Optional[Dict[str, Any]]:
        """Return token account balance (value.amount, value.decimals, value.uiAmount) or None."""
        try:
            result = self._request(
                "getTokenAccountBalance",
                [token_account_pubkey, {"commitment": "confirmed"}],
            )
            return result.get("value")
        except RpcError:
            return None

    def get_token_account_balance_quick(
        self, token_account_pubkey: str, timeout_s: float = 5.0
    ) -> Optional[Dict[str, Any]]:
        """One-shot balance fetch with short timeout so refresh loop cannot block the runner."""
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountBalance",
                "params": [token_account_pubkey, {"commitment": "confirmed"}],
            }
            resp = self._client.post(self._endpoint, json=payload, timeout=timeout_s)
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                return None
            result = data.get("result")
            return result.get("value") if result else None
        except Exception:
            return None

    def get_token_accounts_by_owner(
        self,
        owner: str,
        program_id: str = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    ) -> List[Dict[str, Any]]:
        """
        Fetch token accounts owned by `owner` for a specific token program.
        Defaults to the classic SPL Token program (Tokenkeg).
        """

        params = [
            owner,
            {"programId": program_id},
            {"encoding": "jsonParsed", "commitment": "confirmed"},
        ]
        result = self._request("getTokenAccountsByOwner", params)
        return result["value"]

    def get_signatures_for_address(
        self, address: str, limit: int = 200, before: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        opts: Dict[str, Any] = {"limit": limit}
        if before:
            opts["before"] = before
        params = [address, opts]
        result = self._request("getSignaturesForAddress", params)
        return result

    def get_transaction(self, signature: str) -> Dict[str, Any]:
        params = [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        result = self._request("getTransaction", params)
        return result

    def get_account_info(self, pubkey: str, encoding: str = "base64") -> Optional[Dict[str, Any]]:
        """Return account data or None if not found. Used for mint authority checks."""
        try:
            params = [pubkey, {"encoding": encoding}]
            result = self._request("getAccountInfo", params)
            if result is None:
                return None
            return result
        except RpcError:
            return None

    def send_raw_transaction(self, tx_bytes: bytes) -> str:
        """Single choke point for broadcast. Blocked when TRADING_ENABLED != true (safe mode)."""
        import os

        if os.getenv("TRADING_ENABLED", "").strip().lower() != "true":
            logger.warning("EXECUTION BLOCKED — TRADING HALTED BY CEO DIRECTIVE (TRADING_ENABLED != true)")
            raise RpcError("EXECUTION BLOCKED - TRADING HALTED BY CEO DIRECTIVE")
        import base64

        b64 = base64.b64encode(tx_bytes).decode("utf-8")
        params = [b64, {"encoding": "base64", "skipPreflight": False}]
        result = self._request("sendTransaction", params)
        if isinstance(result, str):
            return result
        return str(result)

    def confirm_transaction(self, signature: str, commitment: str = "confirmed", timeout_s: float = 60.0) -> bool:
        """
        Poll getSignatureStatuses until the transaction reaches the desired commitment or times out.
        """

        start = time.monotonic()
        while True:
            statuses = self._request("getSignatureStatuses", [[signature]])
            value = statuses.get("value") or []
            status = value[0] if value else None
            if status and status.get("confirmationStatus") in (commitment, "finalized"):
                return True
            if (time.monotonic() - start) > timeout_s:
                logger.warning("Timed out waiting for confirmation of %s", signature)
                return False
            time.sleep(2.0)

