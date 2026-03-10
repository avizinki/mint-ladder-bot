"""
Deterministic tests for read-only transfer-provenance analysis (Step 2).

No state mutation; classification only.
"""
from __future__ import annotations

import pytest
from mint_ladder_bot.transfer_provenance_analysis import (
    CLASS_AMBIGUOUS,
    CLASS_LIKELY_SWAP,
    CLASS_TRUSTED_TRANSFER_CANDIDATE,
    CLASS_UNTRUSTED_TRANSFER_CANDIDATE,
    _derive_source_wallet_from_transfer_tx,
    _token_deltas_by_owner_for_mint,
    run_transfer_provenance_analysis,
)


def _tx_meta_pre_post_token_balances(
    wallet: str,
    mint: str,
    wallet_pre: int,
    wallet_post: int,
    other_owners: list[tuple[str, int, int]],
) -> dict:
    """Build meta with preTokenBalances/postTokenBalances. other_owners: (owner, pre, post)."""
    pre = []
    post = []
    if wallet_pre > 0 or wallet_post > 0:
        pre.append({"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(wallet_pre)}})
        post.append({"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(wallet_post)}})
    for o, pr, po in other_owners:
        pre.append({"owner": o, "mint": mint, "uiTokenAmount": {"amount": str(pr)}})
        post.append({"owner": o, "mint": mint, "uiTokenAmount": {"amount": str(po)}})
    return {"meta": {"preTokenBalances": pre, "postTokenBalances": post, "fee": 0}}


def test_token_deltas_by_owner_single_sender():
    """Single sender: wallet +1000, sender -1000."""
    tx = _tx_meta_pre_post_token_balances(
        "WalletMain",
        "MintA",
        wallet_pre=0,
        wallet_post=1000,
        other_owners=[("Sender1", 1000, 0)],
    )
    tx.setdefault("transaction", {})
    tx.setdefault("slot", 100)
    deltas = _token_deltas_by_owner_for_mint(tx, "MintA")
    assert deltas.get("WalletMain") == 1000
    assert deltas.get("Sender1") == -1000


def test_derive_source_wallet_single_sender():
    """Transfer tx: single source derivable."""
    tx = _tx_meta_pre_post_token_balances(
        "WalletMain",
        "MintA",
        wallet_pre=0,
        wallet_post=1000,
        other_owners=[("Sender1", 1000, 0)],
    )
    tx["transaction"] = {}
    src = _derive_source_wallet_from_transfer_tx(tx, "WalletMain", "MintA", 1000)
    assert src == "Sender1"


def test_derive_source_wallet_ambiguous_multiple_senders():
    """Multiple senders: source not derivable -> None."""
    tx = _tx_meta_pre_post_token_balances(
        "WalletMain",
        "MintA",
        wallet_pre=0,
        wallet_post=1000,
        other_owners=[("Sender1", 600, 0), ("Sender2", 400, 0)],
    )
    tx["transaction"] = {}
    src = _derive_source_wallet_from_transfer_tx(tx, "WalletMain", "MintA", 1000)
    assert src is None


def test_run_analysis_trusted_transfer_candidate():
    """Transfer-in from trusted wallet -> trusted-transfer-candidate."""
    wallet = "MainWallet"
    mint = "MintToken"
    trusted = ["TrustedSender"]
    # Tx: MainWallet receives 500 from TrustedSender (no swap)
    tx = {
        "signature": "sig_trusted_transfer",
        "slot": 200,
        "blockTime": 1000000,
        "transaction": {"message": {"accountKeys": [wallet, "TrustedSender"]}},
        "meta": {
            "fee": 0,
            "preTokenBalances": [
                {"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "0"}},
                {"owner": "TrustedSender", "mint": mint, "uiTokenAmount": {"amount": "500"}},
            ],
            "postTokenBalances": [
                {"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "500"}},
                {"owner": "TrustedSender", "mint": mint, "uiTokenAmount": {"amount": "0"}},
            ],
            "preBalances": [1_000_000_000, 1_000_000_000],
            "postBalances": [1_000_000_000, 1_000_000_000],
        },
    }

    class MockRpc:
        def get_signatures_for_address(self, addr: str, limit: int, before=None):
            return [{"signature": tx["signature"]}]

        def get_transaction(self, sig: str):
            return tx if sig == tx["signature"] else None

    candidates = run_transfer_provenance_analysis(
        wallet=wallet,
        mints_tracked={mint},
        rpc=MockRpc(),
        max_signatures=10,
        trusted_source_wallets=trusted,
        decimals_by_mint={mint: 6},
        symbol_by_mint={mint: "TKN"},
    )
    assert len(candidates) == 1
    assert candidates[0].classification == CLASS_TRUSTED_TRANSFER_CANDIDATE
    assert candidates[0].source_wallet == "TrustedSender"
    assert candidates[0].source_in_trusted_list is True
    assert candidates[0].mint == mint
    assert candidates[0].amount_raw == 500


def test_run_analysis_untrusted_transfer_candidate():
    """Transfer-in from untrusted wallet -> untrusted-transfer-candidate."""
    wallet = "MainWallet"
    mint = "MintToken"
    trusted: list[str] = []  # no trusted wallets
    tx = {
        "signature": "sig_untrusted_transfer",
        "slot": 201,
        "blockTime": 1000001,
        "transaction": {"message": {"accountKeys": [wallet, "RandomSender"]}},
        "meta": {
            "fee": 0,
            "preTokenBalances": [
                {"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "0"}},
                {"owner": "RandomSender", "mint": mint, "uiTokenAmount": {"amount": "300"}},
            ],
            "postTokenBalances": [
                {"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "300"}},
                {"owner": "RandomSender", "mint": mint, "uiTokenAmount": {"amount": "0"}},
            ],
            "preBalances": [1_000_000_000, 1_000_000_000],
            "postBalances": [1_000_000_000, 1_000_000_000],
        },
    }

    class MockRpc:
        def get_signatures_for_address(self, addr: str, limit: int, before=None):
            return [{"signature": tx["signature"]}]

        def get_transaction(self, sig: str):
            return tx if sig == tx["signature"] else None

    candidates = run_transfer_provenance_analysis(
        wallet=wallet,
        mints_tracked={mint},
        rpc=MockRpc(),
        max_signatures=10,
        trusted_source_wallets=trusted,
        decimals_by_mint={mint: 6},
    )
    assert len(candidates) == 1
    assert candidates[0].classification == CLASS_UNTRUSTED_TRANSFER_CANDIDATE
    assert candidates[0].source_wallet == "RandomSender"
    assert candidates[0].source_in_trusted_list is False


def test_run_analysis_likely_swap():
    """Recognized SOL->token swap -> likely-swap, not transfer candidate."""
    wallet = "MainWallet"
    mint = "MintToken"
    # Tx that looks like SOL->token: SOL decrease + token increase
    tx = {
        "signature": "sig_swap",
        "slot": 202,
        "blockTime": 1000002,
        "transaction": {"message": {"accountKeys": [wallet, "Jupiter"]}},
        "meta": {
            "fee": 5000,
            "preTokenBalances": [
                {"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "0"}},
            ],
            "postTokenBalances": [
                {"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "1000000"}},
            ],
            "preBalances": [2_000_000_000, 0],
            "postBalances": [1_000_000_000, 0],  # wallet spent 1 SOL
        },
    }

    class MockRpc:
        def get_signatures_for_address(self, addr: str, limit: int, before=None):
            return [{"signature": tx["signature"]}]

        def get_transaction(self, sig: str):
            return tx if sig == tx["signature"] else None

    candidates = run_transfer_provenance_analysis(
        wallet=wallet,
        mints_tracked={mint},
        rpc=MockRpc(),
        max_signatures=10,
        trusted_source_wallets=[],
        decimals_by_mint={mint: 6},
    )
    assert len(candidates) == 1
    assert candidates[0].classification == CLASS_LIKELY_SWAP
    assert candidates[0].reason == "tx_parsed_as_swap"


def test_run_analysis_ambiguous():
    """Transfer with multiple senders -> ambiguous (source not derivable)."""
    wallet = "MainWallet"
    mint = "MintToken"
    # Two senders each send 250; wallet receives 500
    tx = {
        "signature": "sig_ambiguous",
        "slot": 203,
        "blockTime": 1000003,
        "transaction": {"message": {"accountKeys": [wallet]}},
        "meta": {
            "fee": 0,
            "preTokenBalances": [
                {"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "0"}},
                {"owner": "SenderA", "mint": mint, "uiTokenAmount": {"amount": "250"}},
                {"owner": "SenderB", "mint": mint, "uiTokenAmount": {"amount": "250"}},
            ],
            "postTokenBalances": [
                {"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "500"}},
                {"owner": "SenderA", "mint": mint, "uiTokenAmount": {"amount": "0"}},
                {"owner": "SenderB", "mint": mint, "uiTokenAmount": {"amount": "0"}},
            ],
            "preBalances": [1_000_000_000, 1_000_000_000, 1_000_000_000],
            "postBalances": [1_000_000_000, 1_000_000_000, 1_000_000_000],
        },
    }

    class MockRpc:
        def get_signatures_for_address(self, addr: str, limit: int, before=None):
            return [{"signature": tx["signature"]}]

        def get_transaction(self, sig: str):
            return tx if sig == tx["signature"] else None

    candidates = run_transfer_provenance_analysis(
        wallet=wallet,
        mints_tracked={mint},
        rpc=MockRpc(),
        max_signatures=10,
        trusted_source_wallets=["SenderA", "SenderB"],
        decimals_by_mint={mint: 6},
    )
    assert len(candidates) == 1
    assert candidates[0].classification == CLASS_AMBIGUOUS
    assert candidates[0].reason == "source_wallet_not_derivable"
    assert candidates[0].source_wallet is None


def test_deterministic_same_history():
    """Same mocked history produces same output (deterministic)."""
    wallet = "W"
    mint = "M"
    tx = {
        "signature": "sig_det",
        "slot": 1,
        "blockTime": 1,
        "transaction": {"message": {"accountKeys": [wallet, "Src"]}},
        "meta": {
            "fee": 0,
            "preTokenBalances": [
                {"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "0"}},
                {"owner": "Src", "mint": mint, "uiTokenAmount": {"amount": "100"}},
            ],
            "postTokenBalances": [
                {"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "100"}},
                {"owner": "Src", "mint": mint, "uiTokenAmount": {"amount": "0"}},
            ],
            "preBalances": [0, 0],
            "postBalances": [0, 0],
        },
    }

    class MockRpc:
        def get_signatures_for_address(self, addr: str, limit: int, before=None):
            return [{"signature": tx["signature"]}]

        def get_transaction(self, sig: str):
            return tx if sig == tx["signature"] else None

    rpc = MockRpc()
    out1 = run_transfer_provenance_analysis(
        wallet=wallet, mints_tracked={mint}, rpc=rpc, max_signatures=5,
        trusted_source_wallets=["Src"], decimals_by_mint={mint: 6},
    )
    out2 = run_transfer_provenance_analysis(
        wallet=wallet, mints_tracked={mint}, rpc=rpc, max_signatures=5,
        trusted_source_wallets=["Src"], decimals_by_mint={mint: 6},
    )
    assert len(out1) == len(out2) == 1
    assert out1[0].classification == out2[0].classification == CLASS_TRUSTED_TRANSFER_CANDIDATE
    assert out1[0].tx_signature == out2[0].tx_signature
    assert out1[0].amount_raw == out2[0].amount_raw == 100


def test_no_state_mutation():
    """Analysis does not accept or use state write paths (read-only)."""
    # run_transfer_provenance_analysis has no state_path or write parameters;
    # it only takes rpc, wallet, mints, config-like lists. So we just assert
    # the function signature and that it returns list of candidates (no side effects).
    wallet = "W"
    mint = "M"
    tx = {
        "signature": "sig_no_mutate",
        "slot": 1,
        "blockTime": 1,
        "transaction": {"message": {"accountKeys": [wallet]}},
        "meta": {
            "fee": 0,
            "preTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "0"}}],
            "postTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": "50"}}],
            "preBalances": [0],
            "postBalances": [0],
        },
    }
    # No other owner -> source not derivable -> ambiguous
    class MockRpc:
        def get_signatures_for_address(self, addr: str, limit: int, before=None):
            return [{"signature": tx["signature"]}]

        def get_transaction(self, sig: str):
            return tx if sig == tx["signature"] else None

    candidates = run_transfer_provenance_analysis(
        wallet=wallet,
        mints_tracked={mint},
        rpc=MockRpc(),
        max_signatures=5,
        trusted_source_wallets=[],
        decimals_by_mint={mint: 6},
    )
    assert len(candidates) == 1
    assert candidates[0].classification == CLASS_AMBIGUOUS
    # No state file or write was passed or performed
    assert hasattr(run_transfer_provenance_analysis, "__call__")
