from __future__ import annotations

import base64
import os
from typing import Optional

from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction


class WalletError(RuntimeError):
    pass


def load_keypair_from_env(env_var: str = "PRIVATE_KEY_BASE58") -> Keypair:
    """
    Load a Keypair from a base58-encoded secret key stored in an environment variable.

    The variable must contain a 64-byte keypair encoded in base58. The secret
    value is never logged.
    """

    value = os.getenv(env_var)
    if not value:
        raise WalletError(f"{env_var} is not set")
    try:
        return Keypair.from_base58_string(value.strip())
    except Exception as exc:  # pragma: no cover - defensive
        raise WalletError(f"Failed to parse keypair from {env_var}") from exc


def sign_swap_tx(tx_base64: str, keypair: Keypair) -> bytes:
    """
    Sign a base64-encoded versioned transaction using the provided keypair.

    Returns serialized signed transaction bytes suitable for sendRawTransaction.
    """

    tx_bytes = base64.b64decode(tx_base64)
    raw_tx = VersionedTransaction.from_bytes(tx_bytes)
    message = raw_tx.message
    # Must sign the versioned message bytes (with leading version byte), not raw bytes(message)
    msg_bytes = to_bytes_versioned(message)
    signature = keypair.sign_message(msg_bytes)
    signed_tx = VersionedTransaction.populate(message, [signature])
    return bytes(signed_tx)

