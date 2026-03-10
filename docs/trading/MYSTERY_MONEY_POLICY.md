# Mystery Money / Unmatched Balance Delta Policy

**Goal:** Every token balance change must end in exactly one of: (A) matched to real tx and applied, (B) unmatched but informational only with NO tradable lot effects, (C) explicitly ignored as duplicate/replay/stale.

## Allowed outcomes for unmatched balance increase

When wallet balance increases and **no** matching transaction is found:

- **Informational event only:** `UNRESOLVED_BALANCE_DELTA` (one per distinct mint+delta; deduplicated across cycles).
- **State:** May record `last_known_balance_raw` for integrity; **no** new tradable lot, **no** entry, **no** ladder step, **no** sellable increase.
- **Optional later reconciliation:** If the tx is found later (e.g. deeper scan, delayed RPC), resolve and convert into tx-derived lot via normal tx-first or balance-delta tx lookup.

## Never

- Create a phantom tradable buy from an unmatched delta.
- Create a fake entry or sellable from balance delta alone.
- Silently group an unmatched delta into an existing lot.
- Emit duplicate events for the same (mint, delta) every cycle (use stable dedup key `unresolved:{mint}:{delta_raw}`).

## Single source of lot creation

- **Tradable lots** come only from **matched tx parsing** (tx_exact / tx_parsed from tx-first engine or balance-delta tx lookup).
- `BALANCE_DELTA_WITHOUT_TX` / `BUY_DETECTED_NO_TX` / `UNRESOLVED_BALANCE_DELTA` must **never** create a tradable lot.
- `bootstrap_snapshot` and `transfer_received_unknown` remain non-tradable unless explicitly designed otherwise.

## Dedup

- **Per (mint, delta):** Once we report an unresolved delta, we store `unresolved:{mint}:{unmatched_raw}` in safety_state processed_fingerprints and do not re-run tx lookup or re-emit events for that same delta on subsequent cycles.
- **Per (mint, delta, slot):** When we create a lot from a matched tx, we store a slot-based fingerprint so we do not double-create for the same observed delta in the same slot window.

## Reconcile tool

Use `tools/reconcile_balance_delta.py --mint <mint> --from <ts> --to <ts>` to inspect observed deltas, tx search, events, and final classification (matched_tx | unresolved_informational | duplicate_ignored | parser_bug).
