from mint_ladder_bot.tx_infer import (
    _parse_sol_delta_lamports,
    _parse_token_deltas_for_mints,
)


def test_parse_token_deltas_uses_account_index_fallback():
    wallet = "WalletPubkeyTest"
    mint = "MintPubkeyTest"

    # pre/post token balances where owner is not the wallet, but accountIndex
    # points at the wallet's account key. Previous logic (owner-only) would
    # ignore these and return zero delta.
    tx = {
        "transaction": {
            "message": {
                "accountKeys": [wallet, "SomeOtherAccount", "ProgramAccount"],
            }
        },
        "meta": {
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": mint,
                    "owner": "NotTheWallet",
                    "uiTokenAmount": {"amount": "0"},
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": mint,
                    "owner": "NotTheWallet",
                    "uiTokenAmount": {"amount": "1000"},
                }
            ],
        },
    }

    deltas = _parse_token_deltas_for_mints(tx, wallet, [mint])
    assert deltas[mint] == 1000


def test_parse_token_deltas_none_tx_defensive():
    # Passing a None tx should be treated as zero delta, not crash.
    wallet = "WalletPubkeyTest"
    mint = "MintPubkeyTest"
    deltas = _parse_token_deltas_for_mints(None, wallet, [mint])  # type: ignore[arg-type]
    assert deltas[mint] == 0


def test_parse_sol_delta_none_tx_defensive():
    # Passing a None tx should return None, not raise.
    wallet = "WalletPubkeyTest"
    sol_delta = _parse_sol_delta_lamports(None, wallet)  # type: ignore[arg-type]
    assert sol_delta is None

