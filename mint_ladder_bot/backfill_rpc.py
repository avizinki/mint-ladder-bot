"""
Backfill-only RPC client: primary + fallback pool, tx cache, failover on 429/timeout/null.
Used only for tx-backfill; never for live trading execution.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional
from .rpc import RpcClient, RpcError

logger = logging.getLogger(__name__)


def _sanitize_endpoint(url: str) -> str:
    """Redact API keys from URL for logging."""
    return re.sub(r"api-key=[^&\s'\"]+", "api-key=REDACTED", url, flags=re.IGNORECASE)


class BackfillRpcClient:
    """
    RPC client for tx-backfill only. Uses primary RPC then fallback pool on
    429/timeout/null. Caches getTransaction by signature. Throttles requests.
    """

    def __init__(
        self,
        primary_endpoint: str,
        pool_endpoints: List[str],
        timeout_s: float = 20.0,
        delay_after_request_sec: float = 0.2,
        max_retries_per_endpoint: int = 2,
    ) -> None:
        self._primary = primary_endpoint.strip().rstrip("/")
        self._pool = [u.strip().rstrip("/") for u in pool_endpoints if u and u.strip()]
        self._timeout_s = timeout_s
        self._delay_sec = max(0.0, delay_after_request_sec)
        self._max_retries_per_endpoint = max(1, max_retries_per_endpoint)
        # Build ordered list: primary first, then pool
        self._endpoints: List[str] = [self._primary] + self._pool
        # Lazy clients per endpoint (created on first use)
        self._clients: Dict[str, RpcClient] = {}
        # Local tx cache: signature -> tx (never re-fetch)
        self._tx_cache: Dict[str, Any] = {}
        self._log_endpoints_once = True

    def _client_for(self, endpoint: str) -> RpcClient:
        if endpoint not in self._clients:
            self._clients[endpoint] = RpcClient(
                endpoint,
                timeout_s=self._timeout_s,
                max_retries=1,
            )
        return self._clients[endpoint]

    def _throttle(self) -> None:
        if self._delay_sec > 0:
            time.sleep(self._delay_sec)

    def _is_retriable(self, exc: Exception) -> bool:
        """True if we should try next endpoint (429, timeout, null/temporary)."""
        if isinstance(exc, RpcError):
            return True
        msg = str(exc).lower()
        if "429" in msg or "too many requests" in msg:
            return True
        if "timeout" in msg or "timed out" in msg:
            return True
        return False

    def get_transaction(self, signature: str) -> Optional[Dict[str, Any]]:
        """
        Return tx by signature. Uses cache first; on miss, tries primary then pool.
        On 429/timeout/null result, fails over to next endpoint. Logs signature, endpoint, success/failure, retry count.
        """
        if signature in self._tx_cache:
            logger.debug(
                "BACKFILL_RPC get_transaction cache_hit sig=%s",
                signature[:16],
            )
            return self._tx_cache[signature]

        if self._log_endpoints_once:
            logger.info(
                "BACKFILL_RPC endpoints primary=%s pool_count=%d",
                _sanitize_endpoint(self._primary),
                len(self._pool),
            )
            self._log_endpoints_once = False

        total_attempt = 0
        for endpoint in self._endpoints:
            client = self._client_for(endpoint)
            for per_ep_attempt in range(1, self._max_retries_per_endpoint + 1):
                total_attempt += 1
                try:
                    result = client.get_transaction(signature)
                    self._throttle()
                    if result is not None:
                        self._tx_cache[signature] = result
                        logger.info(
                            "BACKFILL_RPC get_transaction sig=%s endpoint=%s success=true retry_count=%d",
                            signature[:16],
                            _sanitize_endpoint(endpoint),
                            total_attempt - 1,
                        )
                        return result
                    # Null result: try next endpoint (temporary/unavailable)
                    logger.info(
                        "BACKFILL_RPC get_transaction sig=%s endpoint=%s success=false reason=null_result retry_count=%d",
                        signature[:16],
                        _sanitize_endpoint(endpoint),
                        total_attempt - 1,
                    )
                    break
                except Exception as exc:
                    self._throttle()
                    logger.info(
                        "BACKFILL_RPC get_transaction sig=%s endpoint=%s success=false error=%s retry_count=%d",
                        signature[:16],
                        _sanitize_endpoint(endpoint),
                        _sanitize_endpoint(str(exc))[:80],
                        total_attempt - 1,
                    )
                    if not self._is_retriable(exc):
                        raise
                    if per_ep_attempt >= self._max_retries_per_endpoint:
                        break
            # If we broke out with no exception but result was None, continue to next endpoint
        return None

    def get_signatures_for_address(
        self, address: str, limit: int = 200, before: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        getSignaturesForAddress with failover across primary then pool.
        No cache. Logs endpoint, success/failure, retry count.
        """
        if self._log_endpoints_once:
            logger.info(
                "BACKFILL_RPC endpoints primary=%s pool_count=%d",
                _sanitize_endpoint(self._primary),
                len(self._pool),
            )
            self._log_endpoints_once = False

        total_attempt = 0
        last_exc: Optional[Exception] = None
        for endpoint in self._endpoints:
            client = self._client_for(endpoint)
            for per_ep_attempt in range(1, self._max_retries_per_endpoint + 1):
                total_attempt += 1
                try:
                    result = client.get_signatures_for_address(address, limit=limit, before=before)
                    self._throttle()
                    if result is not None:
                        logger.info(
                            "BACKFILL_RPC get_signatures_for_address address=%s endpoint=%s success=true retry_count=%d",
                            address[:8],
                            _sanitize_endpoint(endpoint),
                            total_attempt - 1,
                        )
                        return result if isinstance(result, list) else []
                    logger.info(
                        "BACKFILL_RPC get_signatures_for_address address=%s endpoint=%s success=false reason=null retry_count=%d",
                        address[:8],
                        _sanitize_endpoint(endpoint),
                        total_attempt - 1,
                    )
                    break
                except Exception as exc:
                    last_exc = exc
                    self._throttle()
                    logger.info(
                        "BACKFILL_RPC get_signatures_for_address address=%s endpoint=%s success=false error=%s retry_count=%d",
                        address[:8],
                        _sanitize_endpoint(endpoint),
                        _sanitize_endpoint(str(exc))[:80],
                        total_attempt - 1,
                    )
                    if not self._is_retriable(exc):
                        raise
                    if per_ep_attempt >= self._max_retries_per_endpoint:
                        break
        if last_exc is not None:
            raise RpcError(
                f"getSignaturesForAddress failed after {total_attempt} attempts: {_sanitize_endpoint(str(last_exc))}"
            )
        return []

    def close(self) -> None:
        for c in self._clients.values():
            try:
                c.close()
            except Exception:
                pass
        self._clients.clear()

    @property
    def cache_size(self) -> int:
        return len(self._tx_cache)
