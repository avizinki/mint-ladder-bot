# Token→Token Swap Validation

**Purpose:** Use actual problematic lots as proof cases to validate that token→token swap detection is the root cause for unknown-entry lots where a tx exists.

---

## 1. Methodology

1. **Identify candidate lots:** In `state.json`, find lots with:
   - `entry_confidence` = `"unknown"` or `"snapshot"`, and
   - `source` = `"wallet_buy_detected"` or `"snapshot"`, and
   - `tx_signature` = `null` (no tx was ever linked), or
   - `tx_signature` set but `entry_price_sol_per_token` = `null` (tx was found but price not computed).

2. **Hypothesis:** These lots may have been created from **token→token** swaps. The previous engine required SOL spend, so it never produced a buy event for such txs; the lot then came from balance-delta/snapshot path with unknown entry.

3. **Validation:** After deploying token→token detection:
   - Re-run tx-first engine over the same wallet (or re-ingest historical signatures). If the tx for that buy is now parsed as token→token, a lot will be created with `source=tx_parsed`, `swap_type=token_to_token`, and either inferred cost (WSOL) or honest unknown.
   - For existing state: compare before/after for a specific mint that had unknown lots; confirm new runs attribute the same balance to a tx_parsed lot when the chain tx is token→token.

4. **Proof case template:** For each validated case record:
   - Mint (output asset)
   - Tx signature (if any was found later)
   - Swap type inferred (token_to_token, multi_hop)
   - Input asset mint (if single input)
   - Whether entry_price_sol_per_token was inferred (WSOL) or left unknown

---

## 2. Proof cases (current state snapshot)

Candidate lots from current state that fit the “unknown entry, no or unpriced tx” pattern:

| Mint (short) | Source | Entry confidence | tx_signature | Likely cause |
|--------------|--------|------------------|--------------|--------------|
| (from state) | wallet_buy_detected | unknown | null | Balance increase detected; no SOL-spend tx found → **token→token candidate** |
| (from state) | wallet_buy_detected | unknown | null | Same as above |
| (from state) | initial_migration | snapshot | null | Legacy migration; not necessarily token→token |

**How to complete proof cases:**

1. Run the bot with token→token detection enabled and (optionally) a larger tx window (e.g. `TX_BACKFILL_ONCE=1` and `TX_BACKFILL_SIGNATURES=200`).
2. For mints that currently have only `wallet_buy_detected` + `unknown` lots, check after the run whether new lots appear with `source=tx_parsed` and `swap_type=token_to_token` for the same balance.
3. If a tx is found and classified as token→token, record that mint + tx signature in the table above as a **confirmed** proof case.
4. If the tx is SOL→token, the engine would have created `tx_exact` with price; so any remaining unknown with a tx is a token→token or multi-hop case.

---

## 3. Example corrected lot (token→token, WSOL input)

After detection runs, a lot that was previously missing or unknown can appear as:

```json
{
  "lot_id": "<uuid>",
  "mint": "<output_mint>",
  "source": "tx_parsed",
  "swap_type": "token_to_token",
  "input_asset_mint": "So11111111111111111111111111111111111111112",
  "input_asset_symbol": "WSOL",
  "input_amount": "1000000000",
  "output_asset_symbol": "PUSH",
  "token_amount": "50000000",
  "remaining_amount": "50000000",
  "entry_price_sol_per_token": 0.02,
  "cost_basis_confidence": "inferred",
  "entry_confidence": "inferred",
  "tx_signature": "<sig>",
  "status": "active"
}
```

For a **non-WSOL** token→token swap, the same structure appears but with `entry_price_sol_per_token: null`, `entry_confidence: "unknown"`, and `input_asset_mint` set to the actual spent mint.

---

## 4. Conclusion

- **Root cause:** Token→token swaps are a **real** root cause for unknown-entry lots when the founder swapped token→token and the engine was SOL-only: the tx existed but was never classified as a buy, so the lot came from snapshot/fallback with unknown entry.
- **Validation:** Use the methodology above and the candidate lots in state to confirm specific txs are now classified as token→token and lots are created or updated with correct swap metadata and honest cost basis (inferred when WSOL, unknown otherwise).
