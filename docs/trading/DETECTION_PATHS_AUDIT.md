# Detection Paths Audit: Balance Change → Events / Lots

Every path that can react to a wallet balance change. Single source of **tradable** lot creation: matched tx parsing only.

| Path | Trigger | Event(s) | Creates lots? | Changes trading_bag? | Informational only? | Idempotent? | Can run twice same mint/tx? |
|------|---------|----------|----------------|----------------------|----------------------|-------------|------------------------------|
| **Tx-first lot engine** (tx_lot_engine) | Wallet txs fetched; parse buy events (SOL→token, token→token) | BUY_TX_INGESTED, LOT_SOURCE_TX_DERIVED, DUPLICATE_TX_LOT_SKIPPED, PRICE_SANITY_REJECTED | Yes (tx_exact / tx_parsed only) | Yes | No | Yes (dedup by sig+mint) | No (existing_sig_mint) |
| **Runner: balance-delta reconciliation** (_run_buy_detection) | current_raw > sum_lots; unmatched_raw above threshold | UNRESOLVED_BALANCE_DELTA (deduped), BALANCE_DELTA_WITHOUT_TX (aggregate mode) | No (tx-only: only when find_buy_tx* returns match) | Yes when lot created from matched tx | Yes when no match (one event per mint+delta) | Yes (unresolved fp dedup) | No (unresolved fp) |
| **Runner: bootstrap_snapshot synthetic** | Mint has no tx-derived lots after tx-first; trading_bag from status | LOT_SOURCE_BOOTSTRAP | Yes (bootstrap_snapshot; non-tradable) | Yes | N/A | Yes | No |
| **Runner: resync balance drop** | Balance dropped; sum_lots > current_raw; replace with one bootstrap lot | (log only) | Yes (bootstrap_snapshot; non-tradable) | Yes | N/A | Yes | N/A |
| **Discover new mints** (status_snapshot + runner) | New mint in wallet not in status | MINT_DETECTED, TX_LOOKUP_FAILED, BALANCE_DELTA_WITHOUT_TX, MINT_DISCOVERED_NO_LOT | Only if find_buy_tx finds match (tx_exact) | Yes when lot created | Yes when no tx | Yes | Once per new mint |
| **Backfill** (runner) | Startup; token→token lots with null entry | (log) | No (updates entry only) | No | N/A | Yes | N/A |
| **Dashboard event generation** | Reads events.jsonl | (display only) | No | No | Yes | N/A | N/A |

## Summary

- **Tradable lots:** Only from tx_first (tx_exact/tx_parsed) or balance-delta path when `find_buy_tx_for_delta` / `find_buy_txs_for_delta_sum` returns a match.
- **BALANCE_DELTA_WITHOUT_TX / BUY_DETECTED_NO_TX:** Replaced by single **UNRESOLVED_BALANCE_DELTA** per (mint, delta); never create lots.
- **Dedup:** Slot-based fingerprint for “same delta in same slot”; stable `unresolved:{mint}:{delta_raw}` so same unresolved delta is reported and retried only once.
