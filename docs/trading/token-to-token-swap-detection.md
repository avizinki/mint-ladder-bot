# Token→Token Swap Detection

**Purpose:** Extend the lot/tx engine to support SOL→token, token→token, token→SOL, and multi-hop swaps so that lots from token→token buys are detected and cost basis is set correctly (or marked unknown when not reliable).

---

## 1. Swap types supported

| Type | Description | Cost basis |
|------|-------------|------------|
| **SOL → token** | Wallet SOL decreases; tracked mint(s) increase. | `entry_price_sol_per_token` from SOL spent / tokens received. |
| **token → token** | Wallet token A decreases; tracked mint B increases (no SOL spend). | If input is **WSOL**: infer SOL cost (1:1). Otherwise **unknown** (no fake). |
| **token → SOL** | Wallet token decreases; SOL increases. | N/A for *buy* lots (we only create lots for *received* tokens). |
| **multi_hop** | Multiple inputs (e.g. SOL + token) or multiple outputs. | Per received mint: unknown unless single input is WSOL. |

---

## 2. Persisted fields per detected lot

For every detected buy lot the engine persists:

| Field | Description |
|-------|-------------|
| **input_asset_mint** | Mint address of asset spent (optional). |
| **input_asset_symbol** | Symbol when known (from status/tracked mints). |
| **input_amount** | Raw amount spent (string). |
| **output asset** | `mint` + `token_amount` (existing); **output_asset_symbol** when known. |
| **output_asset_symbol** | Symbol for received mint when known. |
| **tx_signature** | Chain tx that caused the buy. |
| **detected_at** | When the buy was detected (block_time or now). |
| **source** | `tx_exact` (SOL→token) or `tx_parsed` (token→token/multi-hop). |
| **cost_basis_confidence** | `known` \| `inferred` \| `unknown`. |

Cost basis:

- **Preferred:** `entry_price_sol_per_token`.
- **Fallback:** `entry_price_usd_per_token` (optional; set when we have USD and not SOL).
- **Neither reliable:** `entry_confidence = unknown`; do **not** fake a value.

---

## 3. Implementation summary

- **tx_infer:** `_parse_token_deltas_for_wallet_all_mints(tx, wallet)` returns token deltas for *all* mints in the tx (not only tracked). Used to see which asset was spent in token→token.
- **tx_lot_engine:** Two paths in `_parse_buy_events_from_tx`: (1) SOL→token when `sol_delta < 0`; (2) token→token when no SOL spend but tracked mint(s) received and some other mint (or WSOL) spent. Single input = WSOL → infer `entry_price_sol_per_token`; else leave unknown.
- **models.LotInfo:** Optional `swap_type`, `input_asset_mint`, `input_asset_symbol`, `input_amount`, `output_asset_symbol`, `entry_price_usd_per_token`.
- **Runner:** Passes `symbol_by_mint` into `run_tx_first_lot_engine` so output and input symbols are set when available.

---

## 4. Root cause for unknown lots

The previous engine was **SOL-centric**: it only produced buy events when the wallet had a **negative SOL delta**. Token→token swaps (no SOL spent) produced **no** buy events, so:

- Those txs were never turned into lots by the tx-first engine, or
- Lots came from snapshot/fallback with **unknown** entry.

So **token→token swaps are a plausible root cause** for unknown-entry lots when a tx exists. The new logic creates lots for token→token and sets cost basis when possible (e.g. WSOL input) or marks `entry_confidence = unknown` honestly.

---

## 5. References

- Implementation report: `docs/trading/TOKEN_TO_TOKEN_SWAP_DETECTION_REPORT.md`
- Validation and proof cases: `docs/trading/token-to-token-validation.md`
