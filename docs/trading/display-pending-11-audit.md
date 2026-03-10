# Audit: 11 display-pending lots

**Date:** 2026-03-08

Display-pending = `entry_confidence === "snapshot"` and `source !== "initial_migration"` (shown as pending in UI).

| # | Mint (symbol) | lot_id (short) | source | token_amount | entry | Why display-pending | Resolvable? | Action |
|---|---------------|-----------------|--------|--------------|-------|----------------------|-------------|--------|
| 1 | x95HN3DWvbf… ($HACHI) | 25ae5404 | wallet_buy_detected | 38867057335012 | null | Snapshot + wallet_buy_detected, no tx match yet | Try tx lookup | Resolve or downgrade |
| 2 | 8opvqaWysX1… (WAR) | 72e85aa9 | wallet_buy_detected | 25533873 | null | Same | Try tx lookup | Resolve or downgrade |
| 3 | 8opvqaWysX1… (WAR) | eb4d0af5 | wallet_buy_detected | 25533873 | null | Same | Try tx lookup | Resolve or downgrade |
| 4 | 8opvqaWysX1… (WAR) | a78edae7 | wallet_buy_detected | 25533873 | null | Same | Try tx lookup | Resolve or downgrade |
| 5 | DMYNp65mub3i… (丙午) | f85e2063 | wallet_buy_detected | 16924422761 | 8.141e-07 | Snapshot + wallet_buy_detected (has inferred entry) | Try tx lookup for exact | Resolve or downgrade |
| 6 | DMYNp65mub3i… (丙午) | f5e4e9bf | wallet_buy_detected | 16895448638 | 8.141e-07 | Same | Try tx lookup | Resolve or downgrade |
| 7 | DMYNp65mub3i… (丙午) | 58429760 | wallet_buy_detected | 33819871399 | null | No entry | Try tx lookup | Resolve or downgrade |
| 8 | DMYNp65mub3i… (丙午) | 2268cc75 | wallet_buy_detected | 33819871399 | null | Same | Try tx lookup | Resolve or downgrade |
| 9 | DMYNp65mub3i… (丙午) | 826be506 | wallet_buy_detected | 33819871399 | null | Same | Try tx lookup | Resolve or downgrade |
| 10 | DMYNp65mub3i… (丙午) | 24d15fdf | wallet_buy_detected | 33819871399 | null | Same | Try tx lookup | Resolve or downgrade |
| 11 | DMYNp65mub3i… (丙午) | 1757f48a | wallet_buy_detected | 33819871399 | null | Same | Try tx lookup | Resolve or downgrade |

**Priorities:** HACHI (1), WAR (2–4), 丙午 / Chinese token (5–11).

**Plan:** Promote all to `entry_confidence=pending_price_resolution`, clear entry so resolver runs, then run resolver once. Resolver will resolve to tx_exact or downgrade to unknown.

---

## Validation (after fix)

| Metric | Before | After |
|--------|--------|-------|
| Display-pending | 11 | **0** |
| Resolved to tx_exact | — | **0** (RPC 429 during resolution pass) |
| Downgraded to unknown | — | **11** |

**Example corrected lot:** mint `x95HN3DWvbfC` ($HACHI), lot_id `25ae5404`, source `wallet_buy_detected` → `entry_confidence=unknown`, `entry_price_sol_per_token=null`. No longer shown as pending in the table.

**Scripts used:**
- `scripts/resolve_display_pending_lots.py` — promotes display-pending to pending_price_resolution and runs resolver (use when RPC is available to try tx_exact resolution).
- `scripts/downgrade_display_pending_to_unknown.py` — sets display-pending lots to unknown without RPC (used to persist the 11 after resolution pass hit rate limits).
