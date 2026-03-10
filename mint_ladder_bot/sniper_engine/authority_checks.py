"""
Token authority checks via RPC — mint_authority and freeze_authority.

SPL Token mint account layout: mint_authority Option<Pubkey> (1+32), supply u64, decimals u8,
is_initialized bool, freeze_authority Option<Pubkey> (1+32). Total 82 bytes.
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# SPL Mint size 82; byte 0 = mint_authority option, byte 43 = freeze_authority option
MINT_AUTHORITY_OPTION_OFFSET = 0
FREEZE_AUTHORITY_OPTION_OFFSET = 43


def parse_mint_authority_options(data: bytes) -> Tuple[bool, bool]:
    """
    Parse SPL Token mint account data. Returns (has_mint_authority, has_freeze_authority).
    If data is too short, returns (True, True) to be conservative (reject).
    """
    if len(data) < 44:
        return True, True
    has_mint = data[MINT_AUTHORITY_OPTION_OFFSET] != 0
    has_freeze = data[FREEZE_AUTHORITY_OPTION_OFFSET] != 0
    return has_mint, has_freeze


def check_mint_authorities(rpc: Any, mint_address: str) -> Tuple[bool, bool, Optional[str]]:
    """
    Fetch mint account via RPC and return (mint_authority_ok, freeze_authority_ok, error_msg).
    mint_authority_ok: True if mint authority is disabled (revoked). False if still active (risk).
    freeze_authority_ok: True if freeze authority is disabled. False if active (risk).
    error_msg: non-None on RPC/parse failure.
    """
    try:
        acc = rpc.get_account_info(mint_address, encoding="base64")
    except Exception as e:
        return False, False, str(e)[:200]
    if not acc:
        return False, False, "account_not_found"
    value = acc.get("value")
    if not value:
        return False, False, "account_value_null"
    b64 = value.get("data")
    if not b64:
        return False, False, "account_data_null"
    try:
        data = base64.b64decode(b64)
    except Exception as e:
        return False, False, "decode_failed"
    has_mint, has_freeze = parse_mint_authority_options(data)
    # OK = authority disabled (no risk)
    mint_ok = not has_mint
    freeze_ok = not has_freeze
    return mint_ok, freeze_ok, None
