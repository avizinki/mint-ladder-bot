from mint_ladder_bot.config import Config
from mint_ladder_bot.models import EntryInfo, MarketInfo, MintStatus, RuntimeMintState
from mint_ladder_bot.strategy import (
    LADDER_MULTIPLES,
    LADDER_PERCENTS,
    build_ladder_for_mint,
    compute_trading_bag,
)


def test_compute_trading_bag_splits_correctly():
    total_raw = 1_000_000  # 1 token with 6 decimals
    trading_bag_raw, moonbag_raw = compute_trading_bag(str(total_raw), trading_bag_pct=0.2)
    assert trading_bag_raw + moonbag_raw == total_raw
    assert trading_bag_raw == int(total_raw * 0.2)


def test_build_ladder_uses_percents_of_trading_bag():
    mint_status = MintStatus(
        mint="mint111",
        token_account="ata111",
        decimals=6,
        balance_ui=10.0,
        balance_raw=str(10_000_000),
        symbol="TEST",
        name="Test Token",
        entry=EntryInfo(entry_price_sol_per_token=0.001),
        market=MarketInfo(),
    )

    trading_bag_raw, moonbag_raw = compute_trading_bag(mint_status.balance_raw, trading_bag_pct=0.2)
    mint_state = RuntimeMintState(
        entry_price_sol_per_token=mint_status.entry.entry_price_sol_per_token,
        trading_bag_raw=str(trading_bag_raw),
        moonbag_raw=str(moonbag_raw),
    )

    steps = build_ladder_for_mint(mint_status, mint_state)
    assert len(steps) == len(LADDER_MULTIPLES) == len(LADDER_PERCENTS)

    total_sold = sum(s.sell_amount_raw for s in steps)
    assert total_sold <= trading_bag_raw
    # At least a reasonable fraction of the bag should be allocated.
    assert total_sold > 0

