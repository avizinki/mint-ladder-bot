"""
Wallet manager: identity, keypair resolution by wallet_id, and signing boundary
with fee-payer check. Implements docs/trading/mint-ladder-bot-wallet-interface.md.
Isolation: no cross-wallet signing; keypair resolution only by wallet_id.
"""
from __future__ import annotations

import base64
from typing import Optional

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from mint_ladder_bot.wallet import (
    WalletError,
    load_keypair_from_env,
    sign_swap_tx,
)


# Default env var for single-wallet (wallet_id None or sentinel)
_DEFAULT_ENV_VAR = "PRIVATE_KEY_BASE58"
# Prefix for per-wallet env: PRIVATE_KEY_BASE58_<wallet_id>
_ENV_VAR_PREFIX = "PRIVATE_KEY_BASE58_"


def _env_var_for_wallet(wallet_id: Optional[str]) -> str:
    """Resolve env var name by convention: PRIVATE_KEY_BASE58 or PRIVATE_KEY_BASE58_<wallet_id>."""
    if wallet_id is None or wallet_id == "":
        return _DEFAULT_ENV_VAR
    return _ENV_VAR_PREFIX + wallet_id


def resolve_keypair(wallet_id: Optional[str] = None) -> Keypair:
    """
    Resolve keypair for the given wallet_id only.

    Convention:
    - PRIVATE_KEY_BASE58_<wallet_id> when wallet_id is set.
    - PRIVATE_KEY_BASE58 as a single-wallet fallback when wallet_id is unset
      or when the wallet-specific var is missing but the default key matches
      the requested wallet_id.

    Never returns a keypair for a different wallet; no key material is logged.
    """
    env_var = _env_var_for_wallet(wallet_id)
    try:
        # Preferred: wallet-specific env var when wallet_id is provided.
        return load_keypair_from_env(env_var)
    except WalletError:
        # For explicit wallet_id, allow a safe fallback to the single-wallet
        # env var when it exists *and* matches the requested wallet_id.
        if wallet_id:
            default_kp = load_keypair_from_env(_DEFAULT_ENV_VAR)
            # Compare string forms to avoid hard dependency on Pubkey in this layer.
            if str(default_kp.pubkey()) != str(wallet_id):
                raise WalletError(
                    f"{env_var} is not set and default key does not match wallet_id"
                )
            return default_kp
        # No wallet_id and wallet-specific env already failed: propagate error.
        raise


def resolve_identity(wallet_id: Optional[str] = None) -> Pubkey:
    """
    Resolve pubkey for the given wallet_id. Ensures keypair is loadable
    and returns its public key. Fails explicitly if wallet_id cannot be
    resolved (e.g. missing env var).
    """
    keypair = resolve_keypair(wallet_id)
    return keypair.pubkey()


def sign_transaction(wallet_id: Optional[str], tx_base64: str) -> bytes:
    """
    Sign a base64-encoded transaction only for the wallet that owns it.

    1) Resolve keypair for wallet_id only.
    2) Decode tx and verify fee payer matches that wallet's pubkey.
    3) If match, sign with that keypair and return signed bytes.
    Does not sign and raises if key missing or fee payer mismatch.
    """
    keypair = resolve_keypair(wallet_id)
    expected_pubkey = keypair.pubkey()

    tx_bytes = base64.b64decode(tx_base64)
    raw_tx = VersionedTransaction.from_bytes(tx_bytes)
    message = raw_tx.message
    # Fee payer is the first account in the message (Solana convention).
    fee_payer = message.account_keys[0]
    if fee_payer != expected_pubkey:
        raise WalletError(
            "Fee payer does not match wallet: will not sign (isolation)"
        )

    return sign_swap_tx(tx_base64, keypair)
