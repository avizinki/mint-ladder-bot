# Token Program / Token-2022 Compatibility

**Date:** 2026-03-08  
**Scope:** Whether unresolved/invalid lot behavior is caused by SPL Token vs Token-2022 differences.

---

## Summary

| Item | Result |
|------|--------|
| Parser (tx_infer, tx_lot_engine) | **Program-agnostic.** Uses RPC `preTokenBalances` / `postTokenBalances`; no filter by program. Solana RPC includes both SPL Token and Token-2022 in these arrays. |
| Discovery (new mints) | **Both programs.** `status_snapshot.discover_new_mints` and `build_status_snapshot` call `get_token_accounts_by_owner` for both `TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA` and `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb`. |
| Balance refresh | **Program-agnostic.** Uses `getTokenAccountBalance(token_account)`; token account address is from status (already resolved). |
| Preflight (main.py) | **SPL only.** Preflight checks token account count with classic program only; undercounts if wallet has only Token-2022. Does not affect runtime. |

---

## Affected mints (problematic lots)

- **丙午** — DMYNp65mub3i7LRpBdB66CgBAceLcQnv4gsWeCi6pump (pump-style; Token-2022).
- Other pump mints in status (e.g. WARBROS, PUSH, WHM, Ditto, etc.) are typically Token-2022.

---

## Root cause of invalid/unresolved lots

**Not** Token-2022 vs SPL. The lot 06a5c25f (丙午) has:

- `source: "tx_exact"`, `tx_signature` set → tx was **found and parsed**.
- `entry_confidence: "unknown"`, `entry_price_sol_per_token: null` → price was **rejected** by `validate_entry_price()` (computed price 3.54e-16 &lt; ENTRY_PRICE_MIN 1e-12) and then cleared by `_downgrade_invalid_exact_lots()`.

So Token-2022 parsing is working; the issue is price sanity (dust-sized SOL / large token amount), not program compatibility.

---

## Optional fix

- **main.py** (`preflight`): Call `get_token_accounts_by_owner` for both Token and Token-2022 and report combined count so preflight is accurate for Token-2022-heavy wallets.
