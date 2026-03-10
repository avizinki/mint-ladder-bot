#!/usr/bin/env python3
"""
Upsert a manual override inventory record for a specific mint into state.json.

Usage: python tools/add_manual_override_record.py

Safe behavior:
- Loads .env so Config paths are correct.
- Loads state.json via load_state.
- Upserts a ManualOverrideRecord for the target mint idempotently.
- Emits MANUAL_OVERRIDE_CREATED only on first creation.
- Saves via save_state_atomic.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path


_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _load_env() -> None:
    for p in (_REPO / ".env", Path(".env")):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()


_load_env()


HACHI_MINT = "x95HN3DWvbfCBtTjGm587z8suK3ec6cwQwgZNLbWKyp"
# Default amount from prior investigation; may be overridden by env for exact activation.
_ENV_AMOUNT = os.getenv("HACHI_MANUAL_OVERRIDE_AMOUNT_RAW")
if _ENV_AMOUNT is not None:
    try:
        HACHI_MANUAL_OVERRIDE_AMOUNT_RAW = int(_ENV_AMOUNT)
    except ValueError:
        HACHI_MANUAL_OVERRIDE_AMOUNT_RAW = 382126968104850
else:
    HACHI_MANUAL_OVERRIDE_AMOUNT_RAW = 382126968104850


def main() -> int:
    from mint_ladder_bot.config import Config
    from mint_ladder_bot.events import append_event, MANUAL_OVERRIDE_CREATED
    from mint_ladder_bot.models import ManualOverrideRecord, RuntimeMintState, StatusFile
    from mint_ladder_bot.state import load_state, save_state_atomic

    cfg = Config()
    state_path = cfg.state_path
    status_path = cfg.status_path

    if not state_path.exists():
        print(f"state.json not found at {state_path}", file=sys.stderr)
        return 1

    state = load_state(state_path, status_path)
    mint_state = state.mints.get(HACHI_MINT)
    if mint_state is None:
        # Minimal mint state; entry/trading_bag will be recomputed by runtime.
        mint_state = RuntimeMintState(
            entry_price_sol_per_token=0.0,
            trading_bag_raw="0",
            moonbag_raw="0",
        )
        state.mints[HACHI_MINT] = mint_state

    # Resolve symbol from status.json if available (cosmetic only).
    symbol = None
    try:
        status = StatusFile.model_validate_json(status_path.read_text())
        for m in status.mints:
            if m.mint == HACHI_MINT:
                symbol = m.symbol
                break
    except Exception:
        symbol = None

    records = getattr(mint_state, "manual_override_inventory", None) or []
    existing = None
    for rec in records:
        if (
            getattr(rec, "mint", None) == HACHI_MINT
            and getattr(rec, "reason", None) == "legacy inventory manual override"
            and getattr(rec, "created_by", None) == "yoav"
        ):
            existing = rec
            break

    created = False
    now = datetime.now(tz=timezone.utc)

    if existing is not None:
        existing.amount_raw = HACHI_MANUAL_OVERRIDE_AMOUNT_RAW
        existing.operator_approved = True
        if existing.created_at is None:
            existing.created_at = now
        if not getattr(existing, "provenance_note", None):
            existing.provenance_note = "provider history incomplete; approved by operator"
        if not getattr(existing, "created_by", None):
            existing.created_by = "yoav"
    else:
        rec = ManualOverrideRecord(
            mint=HACHI_MINT,
            symbol=symbol,
            amount_raw=HACHI_MANUAL_OVERRIDE_AMOUNT_RAW,
            manual_entry_price_sol_per_token=None,
            reason="legacy inventory manual override",
            provenance_note="provider history incomplete; approved by operator",
            operator_approved=True,
            created_at=now,
            created_by="yoav",
        )
        records.append(rec)
        mint_state.manual_override_inventory = records
        created = True

    # Persist state
    save_state_atomic(state_path, state)
    print(f"Manual override upserted for mint {HACHI_MINT[:12]} amount_raw={HACHI_MANUAL_OVERRIDE_AMOUNT_RAW}")

    # Emit audit event only when newly created.
    if created and cfg.event_journal_path is not None:
        try:
            append_event(
                cfg.event_journal_path,
                MANUAL_OVERRIDE_CREATED,
                {
                    "mint": HACHI_MINT[:12],
                    "amount_raw": HACHI_MANUAL_OVERRIDE_AMOUNT_RAW,
                    "reason": "legacy inventory manual override",
                    "provenance_note": "provider history incomplete; approved by operator",
                    "created_by": "yoav",
                },
            )
        except Exception as exc:
            print(f"Warning: failed to append MANUAL_OVERRIDE_CREATED event: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

