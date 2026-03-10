"""
Sniper executor: build swap, execute buy, confirm fill, create lot entry.

Uses Jupiter (SOL->token), existing execution/rpc/wallet. Output: SniperExecutionResult for integration.
Lot creation only after confirm_fill returns actual chain data (no fake state).
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from ..config import Config
from ..jupiter import JupiterError, get_quote, get_swap_tx
from ..rpc import RpcClient, RpcError
from ..tx_infer import parse_buy_fill_from_tx
from ..wallet import WalletError

logger = logging.getLogger(__name__)

WSOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass
class SniperExecutionResult:
    """Result of a sniper buy execution."""

    success: bool
    mint: str
    token_amount_raw: int
    entry_price_sol_per_token: Optional[float] = None
    sol_spent: Optional[float] = None
    tx_signature: Optional[str] = None
    error: Optional[str] = None
    filled_at: Optional[datetime] = None


def build_swap(
    mint: str,
    amount_sol: float,
    config: Config,
    user_pubkey: str,
    slippage_bps: int = 100,
) -> Optional[str]:
    """
    Build swap transaction (SOL → token) via Jupiter.
    Returns base64-encoded swap transaction string, or None on quote/swap failure.
    """
    sol_lamports = int(amount_sol * 1e9)
    if sol_lamports <= 0:
        return None
    try:
        quote = get_quote(
            WSOL_MINT,
            mint,
            sol_lamports,
            slippage_bps,
            config,
        )
        tx_base64 = get_swap_tx(quote, user_pubkey, config)
        return tx_base64
    except JupiterError as e:
        logger.warning("sniper build_swap JupiterError mint=%s: %s", mint[:12], str(e)[:200])
        return None


def execute_buy(
    mint: str,
    amount_sol: float,
    user_pubkey: str,
    sign_fn: Callable[[str], bytes],
    config: Config,
    rpc: RpcClient,
    slippage_bps: int = 100,
    confirm_timeout_s: float = 60.0,
) -> SniperExecutionResult:
    """
    Build, sign, send buy (SOL → token). Returns result with success, tx_sig, or error.
    Does not create lot; caller must confirm_fill then create_lot_in_state.
    """
    tx_base64 = build_swap(mint, amount_sol, config, user_pubkey, slippage_bps)
    if not tx_base64:
        return SniperExecutionResult(
            success=False,
            mint=mint,
            token_amount_raw=0,
            error="build_swap_failed",
        )
    try:
        signed = sign_fn(tx_base64)
    except WalletError as e:
        logger.warning("sniper execute_buy sign failed mint=%s: %s", mint[:12], str(e)[:200])
        return SniperExecutionResult(
            success=False,
            mint=mint,
            token_amount_raw=0,
            error="sign_failed",
        )
    try:
        sig = rpc.send_raw_transaction(signed)
    except RpcError as e:
        logger.warning("sniper execute_buy send failed mint=%s: %s", mint[:12], str(e)[:200])
        return SniperExecutionResult(
            success=False,
            mint=mint,
            token_amount_raw=0,
            error="send_failed",
        )
    confirmed = rpc.confirm_transaction(sig, timeout_s=confirm_timeout_s)
    return SniperExecutionResult(
        success=confirmed,
        mint=mint,
        token_amount_raw=0,
        tx_signature=sig,
        filled_at=datetime.now(tz=timezone.utc) if confirmed else None,
        error=None if confirmed else "confirm_timeout",
    )


def confirm_fill(
    tx_signature: str,
    mint: str,
    wallet: str,
    expected_min_raw: int,
    rpc: RpcClient,
    decimals: int = 6,
) -> tuple[bool, int, Optional[float]]:
    """
    Confirm tx landed and return (success, token_amount_raw, entry_price_sol_per_token).
    Uses get_transaction + parse_buy_fill_from_tx. Returns (False, 0, None) on failure or if
    token received is below expected_min_raw.
    """
    try:
        tx = rpc.get_transaction(tx_signature)
    except Exception as e:
        logger.warning("confirm_fill get_transaction %s failed: %s", tx_signature[:16], e)
        return False, 0, None
    if not tx:
        return False, 0, None
    parsed = parse_buy_fill_from_tx(tx, wallet, mint, decimals)
    if not parsed:
        return False, 0, None
    token_raw, entry_price = parsed
    if token_raw < expected_min_raw:
        logger.info(
            "confirm_fill token_raw %s < expected_min_raw %s",
            token_raw,
            expected_min_raw,
        )
        return False, 0, None
    return True, token_raw, entry_price
