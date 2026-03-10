from datetime import datetime, timezone

from mint_ladder_bot.tx_infer import infer_entries_for_mints


class FakeRpc:
    def __init__(self, tx):
        self._tx = tx

    def get_transaction(self, signature: str):
        return self._tx


def test_infer_single_mint_buy():
    wallet = "wallet1111111111111111111111111111111111111"
    mint = "tokenmint11111111111111111111111111111111111"

    tx = {
        "transaction": {
            "message": {
                "accountKeys": [wallet, "other11111111111111111111111111111111111"],
            }
        },
        "meta": {
            "preBalances": [1_000_000_000, 0],
            "postBalances": [900_000_000, 0],
            "fee": 5_000,
            "preTokenBalances": [
                {
                    "owner": wallet,
                    "mint": mint,
                    "uiTokenAmount": {"amount": "0"},
                }
            ],
            "postTokenBalances": [
                {
                    "owner": wallet,
                    "mint": mint,
                    "uiTokenAmount": {"amount": "1000000"},
                }
            ],
        },
        "blockTime": int(datetime.now(tz=timezone.utc).timestamp()),
    }

    signatures = [{"signature": "sig1"}]
    rpc = FakeRpc(tx)

    entries = infer_entries_for_mints(
        wallet_pubkey=wallet,
        mints=[mint],
        signatures=signatures,
        rpc=rpc,
    )

    assert mint in entries
    entry = entries[mint]
    assert entry.entry_price_sol_per_token > 0
    assert entry.entry_source == "inferred_from_tx"
    assert entry.entry_tx_signature == "sig1"

