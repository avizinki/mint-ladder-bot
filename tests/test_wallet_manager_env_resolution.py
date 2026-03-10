from __future__ import annotations

import pytest

from mint_ladder_bot.wallet import WalletError
from mint_ladder_bot import wallet_manager


class _DummyKeypair:
    def __init__(self, label: str, pubkey_str: str):
        self.label = label
        self._pubkey_str = pubkey_str

    def pubkey(self):
        # Type is intentionally minimal; code uses str(pubkey) only.
        return self._pubkey_str


def test_wallet_specific_env_takes_precedence(monkeypatch):
    calls = []

    def fake_load(env_var: str):
        calls.append(env_var)
        if env_var == "PRIVATE_KEY_BASE58_walletA":
            return _DummyKeypair("specific", "walletA")
        if env_var == "PRIVATE_KEY_BASE58":
            return _DummyKeypair("generic", "walletA")
        raise WalletError(f"{env_var} is not set")

    monkeypatch.setattr(wallet_manager, "load_keypair_from_env", fake_load)

    kp = wallet_manager.resolve_keypair("walletA")
    assert isinstance(kp, _DummyKeypair)
    assert kp.label == "specific"
    # Only wallet-specific env var should be consulted.
    assert calls == ["PRIVATE_KEY_BASE58_walletA"]


def test_generic_fallback_used_when_specific_missing_and_matches(monkeypatch):
    calls = []

    def fake_load(env_var: str):
        calls.append(env_var)
        if env_var == "PRIVATE_KEY_BASE58_walletB":
            raise WalletError(f"{env_var} is not set")
        if env_var == "PRIVATE_KEY_BASE58":
            # Fallback keypair whose pubkey matches the requested wallet_id.
            return _DummyKeypair("generic", "walletB")
        raise WalletError(f"{env_var} is not set")

    monkeypatch.setattr(wallet_manager, "load_keypair_from_env", fake_load)

    kp = wallet_manager.resolve_keypair("walletB")
    assert isinstance(kp, _DummyKeypair)
    assert kp.label == "generic"
    # Both specific and default env vars should be consulted, in that order.
    assert calls == ["PRIVATE_KEY_BASE58_walletB", "PRIVATE_KEY_BASE58"]


def test_generic_fallback_rejected_when_pubkey_mismatch(monkeypatch):
    calls = []

    def fake_load(env_var: str):
        calls.append(env_var)
        if env_var == "PRIVATE_KEY_BASE58_walletC":
            raise WalletError(f"{env_var} is not set")
        if env_var == "PRIVATE_KEY_BASE58":
            # Fallback keypair whose pubkey does NOT match wallet_id.
            return _DummyKeypair("generic", "other_wallet")
        raise WalletError(f"{env_var} is not set")

    monkeypatch.setattr(wallet_manager, "load_keypair_from_env", fake_load)

    with pytest.raises(WalletError) as excinfo:
        wallet_manager.resolve_keypair("walletC")
    assert "default key does not match wallet_id" in str(excinfo.value)
    assert calls == ["PRIVATE_KEY_BASE58_walletC", "PRIVATE_KEY_BASE58"]


def test_missing_both_specific_and_default_env_fails(monkeypatch):
    calls = []

    def fake_load(env_var: str):
        calls.append(env_var)
        raise WalletError(f"{env_var} is not set")

    monkeypatch.setattr(wallet_manager, "load_keypair_from_env", fake_load)

    with pytest.raises(WalletError) as excinfo:
        wallet_manager.resolve_keypair("walletD")
    # The error should reflect that the default key was also unavailable.
    assert "PRIVATE_KEY_BASE58_walletD" in str(excinfo.value) or "PRIVATE_KEY_BASE58" in str(
        excinfo.value
    )
    assert calls == ["PRIVATE_KEY_BASE58_walletD", "PRIVATE_KEY_BASE58"]

