"""
Execution engine — CEO directive §5: isolated execution flow.

Flow: quote → simulate → send → confirm → update state.

- Jupiter swap building
- Simulation before send (RPC sendTransaction with skipPreflight=False)
- Retry logic
- Confirmation tracking

Failures log EXECUTION_FAILED. No execution when TRADING_ENABLED != true (enforced at rpc.send_raw_transaction).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .config import Config
from .jupiter import JupiterError, get_quote, get_swap_tx
from .rpc import RpcClient, RpcError
from .wallet import WalletError, sign_swap_tx

logger = logging.getLogger(__name__)

EXECUTION_FAILED = "EXECUTION_FAILED"


@dataclass
class ExecutionResult:
    success: bool
    signature: Optional[str] = None
    confirmed: bool = False
    error: Optional[str] = None


def _simulate_via_send(rpc: RpcClient, tx_bytes: bytes) -> bool:
    """Simulation is done by RPC when skipPreflight=False. Returns True if no exception."""
    import base64
    b64 = base64.b64encode(tx_bytes).decode("utf-8")
    try:
        rpc._request("sendTransaction", [b64, {"encoding": "base64", "skipPreflight": False}])
        return True
    except RpcError:
        return False


def execute_swap(
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    user_pubkey: str,
    config: Config,
    rpc: RpcClient,
    sign_fn: Callable[[str], bytes],
    max_retries: int = 3,
    confirm_timeout_s: float = 60.0,
) -> ExecutionResult:
    """
    quote → build swap tx → sign → send (simulate via skipPreflight=False) → confirm.
    Returns ExecutionResult; on failure logs EXECUTION_FAILED.
    """
    try:
        quote = get_quote(input_mint, output_mint, amount_raw, config.slippage_bps, config)
        tx_base64 = get_swap_tx(quote, user_pubkey, config)
        signed = sign_fn(tx_base64)
        # Send (simulation happens server-side when skipPreflight=False)
        for attempt in range(1, max_retries + 1):
            try:
                sig = rpc.send_raw_transaction(signed)
                break
            except RpcError as e:
                logger.warning(
                    "%s send_raw_transaction attempt=%d/%d error=%s",
                    EXECUTION_FAILED,
                    attempt,
                    max_retries,
                    str(e)[:200],
                )
                if attempt >= max_retries:
                    return ExecutionResult(success=False, error=str(e))
                time.sleep(1.0 * attempt)
        else:
            return ExecutionResult(success=False, error="no_signature")
        confirmed = rpc.confirm_transaction(sig, timeout_s=confirm_timeout_s)
        return ExecutionResult(success=True, signature=sig, confirmed=confirmed)
    except (JupiterError, WalletError) as e:
        logger.warning("%s %s", EXECUTION_FAILED, str(e)[:200])
        return ExecutionResult(success=False, error=str(e))
    except Exception as e:
        logger.warning("%s %s", EXECUTION_FAILED, str(e)[:200])
        return ExecutionResult(success=False, error=str(e))
