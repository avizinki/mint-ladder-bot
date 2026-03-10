# mint-ladder-bot: Dashboard Update (Recent Buys + Formatting)

**Dev — implementation summary.**

---

## 1. Recent Buys section

- **Location:** New card between summary chips and the main Tracked tokens table.
- **Data source:** `state.json` → `state.mints[mint].lots[]`. Each lot has `detected_at`, `token_amount`, `entry_price_sol_per_token`, `entry_confidence`, `lot_id`, `source`.
- **Build:** `buildRecentBuys(state, status)` flattens all lots across mints, sorts by `detected_at` descending, takes last 25. Symbol/decimals from `status.json` by mint.
- **Render:** `renderRecentBuys(recentBuys)` writes a table: Token (symbol or mint), Amount (formatCompact), Entry (SOL), Detected (time), Lot ID (first 8 chars), Confidence (exact / inferred / snapshot).
- **Events represented:** WALLET_MINT_DISCOVERED, BUY_DETECTED, LOT_CREATED (each created lot appears as one row).

## 2. Smart number formatting

- **formatCompact(value):** Returns "–" for null/NaN; \|n\| ≥ 1e9 → XB; ≥ 1e6 → XM; ≥ 1e3 → XK; else toLocaleString.
- **Where used:** Wallet SOL (if ≥ 1000), on-book value SOL (if ≥ 1000), table columns: balance, position value SOL, trading bag UI, moonbag UI, liquidity USD.

## 3. Dashboard sections (final)

| Section | Content |
|---------|---------|
| Runtime health | RPC latency, state/status/log ok, state.started_at |
| Wallet overview | Wallet address, SOL balance (compact when large) |
| Tracked tokens | Main table: mints, balance, prices, entry, PnL, bag, liquidity, etc. |
| Recent buys | New table: token, amount, entry, timestamp, lot id, confidence |
| Lot state | Per-mint lot data is in state; Recent Buys shows lot-level detection. Full lot list derivable from state in table or future panel. |
| Sell ladder progress | Executed steps, last step, audit (existing) |
| Risk state | Paused, cooldown, errors (existing chips) |
| Alerts / cooldowns | Filters, health badge, run mode (existing) |

## 4. Files changed

- **index.html:** Added `formatCompact()`, `buildRecentBuys()`, `renderRecentBuys()`; added Recent Buys card and host; call `buildRecentBuys`/`renderRecentBuys` in `maybeFinish`; applied `formatCompact` in `renderSummary` and `renderTable` for wallet SOL, on-book value, balance, position value, trading bag, moonbag, liquidity USD.
