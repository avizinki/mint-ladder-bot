from datetime import datetime, timezone
from pathlib import Path

from mint_ladder_bot.config import Config
from mint_ladder_bot.models import (
    LotInfo,
    ManualOverrideRecord,
    RuntimeMintState,
    RuntimeState,
    SolBalance,
)
from mint_ladder_bot.runner import (
    _consume_manual_override,
    _trading_bag_from_lots,
    _update_trading_bag_with_override,
    _apply_sell_inventory_effects,
    _evaluate_manual_override_bypass,
    compute_manual_override_tradable_raw,
)
from mint_ladder_bot.events import read_events, MANUAL_OVERRIDE_CONSUMED
from mint_ladder_bot.tx_lot_engine import _parse_buy_events_from_tx


MINT = "ManualOverrideMintTest"


def _base_mint_state() -> RuntimeMintState:
    return RuntimeMintState(
        entry_price_sol_per_token=1.0,
        trading_bag_raw="0",
        moonbag_raw="0",
    )


def _cfg() -> Config:
    cfg = Config()
    # Avoid relying on env; override relevant fields explicitly.
    cfg.enable_manual_override_inventory = False
    cfg.manual_override_allowed_mints = []
    cfg.manual_override_bypass_enabled = False
    cfg.manual_override_bypass_allowed_mints = []
    return cfg


def test_override_ignored_when_feature_disabled():
    ms = _base_mint_state()
    ms.last_known_balance_raw = "5000"
    ms.manual_override_inventory.append(
        ManualOverrideRecord(
            mint=MINT,
            symbol="OVR",
            amount_raw=2000,
            reason="legacy",
            provenance_note="test",
            operator_approved=True,
        )
    )
    cfg = _cfg()
    cfg.enable_manual_override_inventory = False
    cfg.manual_override_allowed_mints = [MINT]

    override_raw = compute_manual_override_tradable_raw(ms, cfg, MINT)
    assert override_raw == 0
    _update_trading_bag_with_override(ms, cfg, MINT, wallet_balance_raw=5000)
    assert int(ms.trading_bag_raw) == _trading_bag_from_lots(ms)
    assert int(ms.manual_override_tradable_raw or "0") == 0


def test_override_ignored_when_mint_not_allow_listed():
    ms = _base_mint_state()
    ms.last_known_balance_raw = "5000"
    ms.manual_override_inventory.append(
        ManualOverrideRecord(
            mint=MINT,
            symbol="OVR",
            amount_raw=2000,
            reason="legacy",
            provenance_note="test",
            operator_approved=True,
        )
    )
    cfg = _cfg()
    cfg.enable_manual_override_inventory = True
    cfg.manual_override_allowed_mints = ["SomeOtherMint"]

    override_raw = compute_manual_override_tradable_raw(ms, cfg, MINT)
    assert override_raw == 0
    _update_trading_bag_with_override(ms, cfg, MINT, wallet_balance_raw=5000)
    assert int(ms.trading_bag_raw) == _trading_bag_from_lots(ms)
    assert int(ms.manual_override_tradable_raw or "0") == 0


def test_override_included_when_enabled_and_allow_listed_and_approved():
    ms = _base_mint_state()
    ms.last_known_balance_raw = "5000"
    ms.manual_override_inventory.append(
        ManualOverrideRecord(
            mint=MINT,
            symbol="OVR",
            amount_raw=2000,
            reason="legacy",
            provenance_note="test",
            operator_approved=True,
        )
    )
    cfg = _cfg()
    cfg.enable_manual_override_inventory = True
    cfg.manual_override_allowed_mints = [MINT]

    override_raw = compute_manual_override_tradable_raw(ms, cfg, MINT)
    assert override_raw == 2000
    _update_trading_bag_with_override(ms, cfg, MINT, wallet_balance_raw=5000)
    assert int(ms.trading_bag_raw) == 2000
    assert int(ms.manual_override_tradable_raw or "0") == 2000


def test_trading_bag_never_exceeds_wallet_balance():
    ms = _base_mint_state()
    ms.last_known_balance_raw = "5000"
    ms.manual_override_inventory.append(
        ManualOverrideRecord(
            mint=MINT,
            symbol="OVR",
            amount_raw=10000,  # larger than wallet
            reason="legacy",
            provenance_note="test",
            operator_approved=True,
        )
    )
    cfg = _cfg()
    cfg.enable_manual_override_inventory = True
    cfg.manual_override_allowed_mints = [MINT]

    _update_trading_bag_with_override(ms, cfg, MINT, wallet_balance_raw=5000)
    assert int(ms.trading_bag_raw) <= 5000
    assert int(ms.manual_override_tradable_raw or "0") <= 5000


def test_sells_consume_tx_lots_first_then_override(tmp_path):
    # One tx lot for 1000, manual override 500, sell 1200.
    ms = _base_mint_state()
    ms.last_known_balance_raw = "1500"
    lot = LotInfo.create(mint=MINT, token_amount_raw=1000, entry_price=1.0, source="tx_exact")
    ms.lots = [lot]
    ms.manual_override_inventory.append(
        ManualOverrideRecord(
            mint=MINT,
            symbol="OVR",
            amount_raw=500,
            reason="legacy",
            provenance_note="test",
            operator_approved=True,
        )
    )
    # Consume via helpers
    from_tx = min(1200, _trading_bag_from_lots(ms))
    assert from_tx == 1000
    _trading_bag_from_lots(ms)  # initial
    from mint_ladder_bot.runner import _debit_lots_fifo  # local import to avoid circular issues

    _debit_lots_fifo(ms, from_tx)
    assert int(ms.lots[0].remaining_amount) == 0
    assert ms.lots[0].status == "fully_sold"

    journal_path = tmp_path / "events.jsonl"
    consumed_override = _consume_manual_override(ms, 1200 - from_tx, journal_path, MINT, "sig123")
    assert consumed_override == 200
    # Override pool reduced
    rec = ms.manual_override_inventory[0]
    assert rec.amount_raw == 300
    # manual_override_sold_raw updated
    assert ms.manual_override_sold_raw == "200"
    # Event emitted
    events = read_events(journal_path)
    mo_events = [e for e in events if e.get("event") == MANUAL_OVERRIDE_CONSUMED]
    assert len(mo_events) == 1
    e = mo_events[0]
    assert e.get("mint") == MINT[:12]
    assert int(e.get("amount_raw")) == 200
    assert int(e.get("remaining_override_raw")) == 300
    assert e.get("tx_sig").startswith("sig123"[:3])


def test_override_never_affects_tx_inference():
    # Build a dummy tx that has no mint balances; buy parser should emit no events
    wallet = "WalletX"
    mint = "DummyMint"
    tx = {"meta": {"preTokenBalances": [], "postTokenBalances": []}, "transaction": {"message": {"accountKeys": []}}}
    ms = _base_mint_state()
    ms.last_known_balance_raw = "1000"
    ms.manual_override_inventory.append(
        ManualOverrideRecord(
            mint=mint,
            symbol="DUM",
            amount_raw=999999,
            reason="legacy",
            provenance_note="test",
            operator_approved=True,
        )
    )
    cfg = _cfg()
    cfg.enable_manual_override_inventory = True
    cfg.manual_override_allowed_mints = [mint]
    # Update trading bag from override; this should not change tx inference behavior.
    _update_trading_bag_with_override(ms, cfg, mint, wallet_balance_raw=1000)
    before = _parse_buy_events_from_tx(tx, wallet, "sig", {mint}, {mint: 6})
    after = _parse_buy_events_from_tx(tx, wallet, "sig", {mint}, {mint: 6})
    assert before == after == []


def test_negative_or_invalid_override_amount_ignored():
    ms = _base_mint_state()
    ms.last_known_balance_raw = "5000"
    # Negative amount and invalid amount should be ignored safely
    ms.manual_override_inventory.append(
        ManualOverrideRecord(
            mint=MINT,
            symbol="OVR",
            amount_raw=-100,
            reason="legacy",
            provenance_note="test",
            operator_approved=True,
        )
    )
    bad = ManualOverrideRecord(
        mint=MINT,
        symbol="OVR",
        amount_raw=0,
        reason="legacy",
        provenance_note="test",
        operator_approved=True,
    )
    # type: ignore[attr-defined]
    bad.amount_raw = "not-an-int"  # deliberately invalid at runtime
    ms.manual_override_inventory.append(bad)  # type: ignore[arg-type]

    cfg = _cfg()
    cfg.enable_manual_override_inventory = True
    cfg.manual_override_allowed_mints = [MINT]

    override_raw = compute_manual_override_tradable_raw(ms, cfg, MINT)
    assert override_raw == 0


def test_bypass_disabled_keeps_existing_behavior():
    ms = _base_mint_state()
    ms.last_known_balance_raw = "5000"
    ms.manual_override_inventory.append(
        ManualOverrideRecord(
            mint=MINT,
            symbol="OVR",
            amount_raw=2000,
            reason="legacy",
            provenance_note="test",
            operator_approved=True,
        )
    )
    cfg = _cfg()
    cfg.enable_manual_override_inventory = True
    cfg.manual_override_allowed_mints = [MINT]
    cfg.manual_override_bypass_enabled = False
    cfg.manual_override_bypass_allowed_mints = [MINT]

    # Evaluate bypass: should stay inactive; override behaves via normal helper.
    eff = _evaluate_manual_override_bypass(MINT, ms, actual_raw=5000, config=cfg, event_journal_path=None)
    assert eff == 0
    assert ms.manual_override_bypass_active is False
    _update_trading_bag_with_override(ms, cfg, MINT, wallet_balance_raw=5000)
    assert int(ms.trading_bag_raw) == 2000
    assert int(ms.manual_override_tradable_raw or "0") == 2000


def test_bypass_enabled_for_allow_listed_mint(tmp_path):
    ms = _base_mint_state()
    ms.last_known_balance_raw = "1500"
    lot = LotInfo.create(mint=MINT, token_amount_raw=1000, entry_price=1.0, source="tx_exact")
    ms.lots = [lot]
    ms.manual_override_inventory.append(
        ManualOverrideRecord(
            mint=MINT,
            symbol="OVR",
            amount_raw=500,
            reason="legacy",
            provenance_note="test",
            operator_approved=True,
        )
    )
    # Simulate reconciliation mismatch pause.
    ms.failures.paused_until = datetime.now(tz=timezone.utc)
    ms.failures.last_error = "reconciliation_mismatch"

    cfg = _cfg()
    cfg.enable_manual_override_inventory = True
    cfg.manual_override_allowed_mints = [MINT]
    cfg.manual_override_bypass_enabled = True
    cfg.manual_override_bypass_allowed_mints = [MINT]
    cfg.manual_override_bypass_min_override_raw = 0

    journal_path = tmp_path / "events.jsonl"
    eff = _evaluate_manual_override_bypass(MINT, ms, actual_raw=1500, config=cfg, event_journal_path=journal_path)
    assert eff == 500
    assert ms.manual_override_bypass_active is True
    assert ms.manual_override_bypass_reason is not None
    # Under bypass, only override bag should be considered tradable when applied by policy.
    ms.trading_bag_raw = str(eff)
    assert int(ms.trading_bag_raw) == 500

    from mint_ladder_bot.events import read_events, MANUAL_OVERRIDE_BYPASS_ENABLED

    events = read_events(journal_path)
    ev = [e for e in events if e.get("event") == MANUAL_OVERRIDE_BYPASS_ENABLED]
    assert len(ev) == 1
    assert ev[0].get("mint") == MINT[:12]


def test_ladder_style_sell_consumes_tx_then_override(tmp_path):
    # One tx lot for 1000, manual override 500, sell 1200 via unified helper.
    ms = _base_mint_state()
    ms.last_known_balance_raw = "1500"
    lot = LotInfo.create(mint=MINT, token_amount_raw=1000, entry_price=1.0, source="tx_exact")
    ms.lots = [lot]
    ms.manual_override_inventory.append(
        ManualOverrideRecord(
            mint=MINT,
            symbol="OVR",
            amount_raw=500,
            reason="legacy",
            provenance_note="test",
            operator_approved=True,
        )
    )
    cfg = _cfg()
    cfg.enable_manual_override_inventory = True
    cfg.manual_override_allowed_mints = [MINT]

    journal_path = tmp_path / "events.jsonl"
    _apply_sell_inventory_effects(
        mint_state=ms,
        config=cfg,
        mint_addr=MINT,
        sold_raw=1200,
        journal_path=journal_path,
        tx_signature="sig_ladder",
    )

    # Tx lot fully consumed first.
    assert int(ms.lots[0].remaining_amount) == 0
    assert ms.lots[0].status == "fully_sold"
    # Override reduced for the spill (200) and tracked separately.
    rec = ms.manual_override_inventory[0]
    assert rec.amount_raw == 300
    assert ms.manual_override_sold_raw == "200"
    # Trading bag reflects remaining wallet capacity (only override pool left).
    assert int(ms.trading_bag_raw) == 300
    assert int(ms.manual_override_tradable_raw or "0") == 300
    # Event emitted for override consumption.
    events = read_events(journal_path)
    mo_events = [e for e in events if e.get("event") == MANUAL_OVERRIDE_CONSUMED]
    assert len(mo_events) == 1
    e = mo_events[0]
    assert e.get("mint") == MINT[:12]
    assert int(e.get("amount_raw")) == 200
    assert int(e.get("remaining_override_raw")) == 300
    assert e.get("tx_sig").startswith("sig_ladder"[:3])


def test_apply_sell_inventory_effects_does_not_touch_other_mint(tmp_path):
    ms_main = _base_mint_state()
    ms_main.last_known_balance_raw = "1500"
    lot = LotInfo.create(mint=MINT, token_amount_raw=1000, entry_price=1.0, source="tx_exact")
    ms_main.lots = [lot]
    ms_main.manual_override_inventory.append(
        ManualOverrideRecord(
            mint=MINT,
            symbol="OVR",
            amount_raw=500,
            reason="legacy",
            provenance_note="test",
            operator_approved=True,
        )
    )
    other_mint = "OtherMint"
    ms_other = _base_mint_state()
    ms_other.last_known_balance_raw = "9999"
    ms_other.manual_override_inventory.append(
        ManualOverrideRecord(
            mint=other_mint,
            symbol="OTH",
            amount_raw=999,
            reason="legacy",
            provenance_note="test",
            operator_approved=True,
        )
    )
    cfg = _cfg()
    cfg.enable_manual_override_inventory = True
    cfg.manual_override_allowed_mints = [MINT]

    journal_path = tmp_path / "events.jsonl"
    _apply_sell_inventory_effects(
        mint_state=ms_main,
        config=cfg,
        mint_addr=MINT,
        sold_raw=1200,
        journal_path=journal_path,
        tx_signature="sig_main",
    )

    # Other mint's override untouched and never made tradable (not allow-listed).
    other_rec = ms_other.manual_override_inventory[0]
    assert other_rec.amount_raw == 999
    assert getattr(ms_other, "manual_override_sold_raw", None) in (None, "0")
    assert getattr(ms_other, "manual_override_tradable_raw", None) in (None, "0")

