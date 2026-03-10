# Pending Lot Breakdown

**Owner:** Dev + DevOps  
**Date:** 2026-03-08  
**Scope:** For every lot shown as pending, determine exact reason it is still pending.

---

## Current state (state.json snapshot)

- **Lots with `entry_confidence=pending_price_resolution`:** **0**
- **Lots displayed as pending in UI (display-pending):** **9**  
  These have `entry_confidence=snapshot`, `source=wallet_buy_detected`, and `entry_price_sol_per_token` null or zero.

---

## Why “pending” count differs from stored confidence

- New wallet_buy_detected lots are created with `entry_confidence=pending_price_resolution` so the resolver can try to find a tx.
- Some code paths (e.g. reconciliation resync, or older runs) can leave lots as `snapshot` with `source=wallet_buy_detected` and no entry. The UI treats those as “pending” for display.
- So:
  - **Stored in state:** 0 with `pending_price_resolution`, 9 with snapshot + wallet_buy_detected + no entry.
  - **Shown in dashboard:** 9 “pending” (using the display-pending rule).

---

## Failure categories for display-pending lots

For the **9 display-pending lots** (snapshot + wallet_buy_detected + no entry), the reason they have no entry is one of:

| Category | Description |
|----------|-------------|
| **resolver_not_reached** | Resolver was previously only run when `balances_refresh` was non-empty; when RPC failed, resolver did not run, so these lots were never attempted. (Fixed: resolver now runs every cycle.) |
| **tx_not_found** | Resolver ran but `find_buy_tx_for_delta` found no matching tx in the scan window. |
| **delta_mismatch** | Tx(s) found but token delta did not match lot amount (within tolerance). |
| **scan_window_exceeded** | Scan reached `ENTRY_SCAN_MAX_SIGNATURES` without a match. |
| **rpc_error** | get_signatures_for_address or get_transaction failed. |
| **tx_found_but_invalid_price** | Tx matched but computed price failed sanity (e.g. &lt; ENTRY_PRICE_MIN). |
| **dashboard_state_mismatch** | State stores `snapshot` but UI shows as pending; count aligned via display-pending rule. |

---

## Per-lot breakdown (current state)

With **0** lots stored as `pending_price_resolution`, there are no rows to list with a stored “pending” state. The **9** display-pending lots are snapshot + wallet_buy_detected; their “still pending” reason is **resolver_not_reached** (resolver was skipped when balance refresh failed) or **tx_not_found** / **scan_window_exceeded** from earlier runs before they were overwritten to snapshot (e.g. by reconciliation).

To get an exact per-lot failure category for current display-pending lots:

1. Run `scripts/report_tx_lookup_failures.py` on a state where those lots are stored as `pending_price_resolution`, or  
2. After ensuring new wallet_buy_detected lots are stored as `pending_price_resolution`, run the bot and grep logs for `RESOLVER_STATUS`, `TX_LOOKUP_FAILED`, `PENDING_LOT_RESOLVED`, `PRICE_SANITY_REJECTED`.

---

## Recommendation

- Resolver now runs every cycle, so new pending lots will be attempted every cycle.
- For the 9 display-pending lots: either re-tag them to `pending_price_resolution` (if desired) so the resolver retries them, or leave as snapshot and accept that the dashboard “pending” count (9) reflects display-pending and matches the UI.
