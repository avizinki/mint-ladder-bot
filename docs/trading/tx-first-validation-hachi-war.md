# TX-First Validation: HACHI, WAR, PUSH, 丙午 (Real Data)

**Data source:** state.json, status.json as of current workspace. Wallet: `3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR`.

---

## 1. HACHI ($HACHI)

**Mint:** `x95HN3DWvbfCBtTjGm587z8suK3ec6cwQwgZNLbWKyp`  
**Decimals:** 4

### A. Wallet / Jupiter history (from state + executed_steps)

- **Sell (on-chain):** sig `2e47PPvhgnwg4mQYDP5a9asTH13wYy9oGgJ78nnpJUCURGWqyTkY3a3XVwXUJ2c4xET2zYnWiiR9jbwsMhJ7Y4to`, time 2026-03-08T01:39:39Z, sold_raw 529657061807, sol_out 0.002857737.
- **Buy (tx_exact):** sig `2gXvtRb9zGQxAXoxcDzp73LQ54UUqKq8YZvRYTAh98wT58rZAM1SFMhyAPU5RPpCXDwXiqyPh1Gftx23wuCw17JU`, detected_at 2026-03-07T20:46:16Z, amount 1855498324498, entry 5.3926e-11 SOL/token.

### B. State explanation

| Source | Lot ID | remaining_amount | tx_signature |
|--------|--------|------------------|--------------|
| **tx_exact** | 2a8dd111-c451-4d95-89b0-07ab3c3776c2 | 1,855,498,324,498 | 2gXvtRb9zGQxAXoxcDzp73LQ54UUqKq8YZvRYTAh98wT... |
| **initial_migration** | 1a84b584-3746-4948-85cb-dc8ab4b62a6a | 170,610,612,001,678 | null |
| **wallet_buy_detected** | 25ae5404-dde2-4d45-90c6-2b44c9404b27 | 38,867,057,335,012 | null |

- **tx_derived_raw:** 1,855,498,324,498  
- **bootstrap_snapshot_raw (initial_migration):** 170,610,612,001,678  
- **transfer_received_unknown (wallet_buy_detected):** 38,867,057,335,012  
- **sold_raw:** 529,657,061,807  

### C. Dashboard explanation

- **token_holdings_breakdown:** tx_derived_raw = 1,855,498,324,498; bootstrap_snapshot_raw = 170,610,612,001,678; transfer_unknown_raw = 38,867,057,335,012; sold_raw = 529,657,061,807; sum_active_lots_raw = 211,333,167,661,188.
- **Visible lots:** 3 (one tx_exact, one initial_migration, one wallet_buy_detected). source_category: tx_derived, bootstrap, transfer_unknown.

### D. Reconciliation proof

- **status.json balance_raw (current_balance):** 381,147,595,461,982  
- **sum_active_lots:** 211,333,167,661,188  
- **Formula check:** tx_derived + bootstrap + transfer_unknown − sold = 1,855,498,324,498 + 170,610,612,001,678 + 38,867,057,335,012 − 529,657,061,807 = 211,333,167,661,188.  
- **Mismatch:** current_balance (381,147,595,461,982) ≠ sum_active_lots (211,333,167,661,188). **Gap = 169,814,427,800,794** (wallet has more than state’s lots).

### E. Trust verdict

**Partially explained.**  
- 211B of 381B is explained by state lots (one tx_exact, one initial_migration, one wallet_buy_detected) and one sell.  
- **Unexplained gap:** 169.8B raw. Root cause: wallet balance (status snapshot) exceeds sum of all active lots; likely additional on-chain receives/buys not yet represented as lots, or status captured a later balance than state’s last_known. No fabrication of lots; gap should be closed by tx-first ingestion of missing txs or marked as transfer_unknown once confirmed.

---

## 2. WAR

**Mint:** `8opvqaWysX1oYbXuTL8PHaoaTiXD69VFYAX4smPebonk`  
**Decimals:** 6

### A. Wallet / Jupiter history (from state)

- **Buy (tx_exact):** sig `2xJQAMXYRFTx6dxegmUjMWLwCk1yWoWME4Qc32JfCzyfRQeFgLFuJ5CY7kNMTtVHddqzSscRmZ77kbm4R8fhcW32`, detected_at 2026-03-07T22:33:29Z, amount 25,533,873, entry 2.35e-7 SOL/token.
- No executed_steps (no sells in state).

### B. State explanation

| Source | Lot ID | remaining_amount | tx_signature |
|--------|--------|------------------|--------------|
| **tx_exact** | a34a8398-c519-423a-a9c1-89084c2cc80d | 25,533,873 | 2xJQAMXYRFTx6dxegmUjMWLwCk1yWoWME4Qc32JfCzyf... |
| **initial_migration** | 702c58b3-dd28-4ede-9cab-51a257d676b0 | 1,077,389,343 | null |
| **wallet_buy_detected** | 72e85aa9-0b47-46cf-b1eb-2edce00c51af | 25,533,873 | null |
| **wallet_buy_detected** | eb4d0af5-db76-4d71-bc31-5a7791a2a09f | 25,533,873 | null |
| **wallet_buy_detected** | a78edae7-0d39-4344-99f2-c6fbc2b48fe5 | 25,533,873 | null |

- **tx_derived_raw:** 25,533,873  
- **bootstrap_snapshot_raw:** 1,077,389,343  
- **transfer_unknown_raw:** 76,601,619 (3 × 25,533,873)  
- **sold_raw:** 0  
- **sum_active_lots:** 1,179,524,835  

### C. Dashboard explanation

- **token_holdings_breakdown:** tx_derived_raw = 25,533,873; bootstrap_snapshot_raw = 1,077,389,343; transfer_unknown_raw = 76,601,619; sold_raw = 0; sum_active_lots_raw = 1,179,524,835.
- **Visible lots:** 5; source_category: one tx_derived, one bootstrap, three transfer_unknown.

### D. Reconciliation proof

- **status.json balance_raw (current_balance):** 2,154,778,686  
- **sum_active_lots:** 1,179,524,835  
- **Formula:** tx_derived + bootstrap + transfer_unknown − sold = 25,533,873 + 1,077,389,343 + 76,601,619 − 0 = 1,179,524,835.  
- **Mismatch:** current_balance (2,154,778,686) ≠ sum_active_lots (1,179,524,835). **Gap = 975,253,851** (wallet has more than state’s lots). Duplicate wallet_buy_detected lots (same amount 25,533,873) suggest one real tx and three balance-delta duplicates; extra 975M raw not explained by any lot.

### E. Trust verdict

**Partially explained.**  
- One tx_exact lot matches a real swap; one initial_migration; three wallet_buy_detected lots (legacy duplicates).  
- **Unexplained gap:** 975,253,851 raw. Root cause: (1) wallet balance higher than sum of lots; (2) duplicate wallet_buy_detected lots for same delta (one tx_exact + three balance-delta copies). Fix: stop creating wallet_buy_detected (done); merge or mark duplicate lots so one delta = one lot; ingest missing txs for the 975M increase.

---

## 3. PUSH (third token with real activity)

**Mint:** `F5RyYFSXMuTXPjcLBTDr9ihy7ZUw93f1b3gLn1U1pump`  
**Decimals:** 6  
**Symbol:** PUSH

### A. Wallet / Jupiter history (from state)

- **Buy (tx_exact):** sig `QpEJ4QPMBn4HwnDFkTyp9XcMgsSGwo8ks6ahdq98a7XMrLcHCHo5nbitwnGsAPQT7NnzDvXqan5cuMiBJbGnM4C`, detected_at 2026-03-07T21:03:10Z, amount 953,797,634,107, entry 6.29e-12 SOL/token.
- **Sell:** sig `5hGpAdpmzaBnFLPkUPMHRX89pidP1fBYSJnbhrAS4tf7hEGHcL27Zbmcm4uwPfcmpuUw8e6Ba2bFQ4kRXjW5jDua`, time 2026-03-08T01:16:22Z, sold_raw 18,529,785,687.

### B. State explanation

| Source | Lot ID | remaining_amount | tx_signature |
|--------|--------|------------------|--------------|
| **tx_exact** | 0397107d-87a6-439f-9df2-b29daf1255ae | 953,797,634,107 | QpEJ4QPMBn4HwnDFkTyp9XcMgsSGwo8ks6ahdq98a7X... |
| **initial_migration** | ded21919-538e-4c30-92ae-3d2a6ed3c2ec | 6,458,116,640,978 | null |

- **tx_derived_raw:** 953,797,634,107  
- **bootstrap_snapshot_raw:** 6,458,116,640,978  
- **transfer_unknown_raw:** 0  
- **sold_raw:** 18,529,785,687  
- **sum_active_lots:** 7,411,914,275,085  

### C. Dashboard explanation

- **token_holdings_breakdown:** tx_derived_raw = 953,797,634,107; bootstrap_snapshot_raw = 6,458,116,640,978; transfer_unknown_raw = 0; sold_raw = 18,529,785,687; sum_active_lots_raw = 7,411,914,275,085.
- **Visible lots:** 2 (one tx_exact, one initial_migration). source_category: tx_derived, bootstrap.

### D. Reconciliation proof

- **status.json balance_raw (current_balance):** 12,916,233,281,956  
- **sum_active_lots:** 7,411,914,275,085  
- **Formula:** tx_derived + bootstrap + transfer_unknown − sold = 953,797,634,107 + 6,458,116,640,978 − 18,529,785,687 = 7,393,384,489,398 (remaining after sell). State’s sum_active_lots (7,411,914,275,085) is pre-sell sum of remainings; sold was debited from a lot so remaining totals 7.41B.  
- **Mismatch:** current_balance (12,916,233,281,956) ≠ sum_active_lots (7,411,914,275,085). **Gap = 5,504,319,016,871** (wallet has more than state’s lots—likely more buys or receives not yet in state).

### E. Trust verdict

**Partially explained.**  
- One tx_exact and one initial_migration explain 7.41B; one sell (18.5M) is recorded.  
- **Unexplained gap:** 5.5B raw. Root cause: wallet balance (12.9B) exceeds sum of lots (7.4B); additional on-chain activity not yet represented as lots. No duplicate wallet_buy_detected for PUSH; structure is clean. Fix: tx-first (or backfill) must pick up missing txs for the 5.5B increase.

---

## Summary table (real data)

| Token | current_balance (status) | sum_active_lots | sold_raw | Gap | Verdict |
|-------|--------------------------|-----------------|----------|-----|--------|
| HACHI | 381,147,595,461,982 | 211,333,167,661,188 | 529,657,061,807 | 169,814,427,800,794 | Partially explained |
| WAR | 2,154,778,686 | 1,179,524,835 | 0 | 975,253,851 | Partially explained |
| PUSH | 12,916,233,281,956 | 7,411,914,275,085 | 18,529,785,687 | 5,504,319,016,871 | Partially explained |

---

## 4. 丙午 (bǐngwǔ)

**Mint:** `DMYNp65mub3i7LRpBdB66CgBAceLcQnv4gsWeCi6pump`  
**Decimals:** 6

### A. Wallet / Jupiter history (from state)

- **tx_exact buys:** sig `4kfvhCMVBtesL3EWWxkfyKJAht9M8mtn6u6Q6r1nZonCKHpesR5AJkTGTuu4UFKqW1f2rcdAnu8hT4KGKZFZY4EK` (detected 2026-03-07T21:24:57Z), amount 16,924,422,761; sig `4cYh5jKNVMrfjxA28gjtoXwFMNJj5jxP7UqdyLTageNnFFAhusVtxDbGu1wbocuPemrckmoq8xFxWuBHeV6yWpbG` (detected 2026-03-07T22:16:02Z), amount 16,895,448,638.
- **No executed_steps** (no sells in state).

### B. State explanation

- **tx_derived_raw:** 33,819,871,399 (two tx_exact lots: 16,924,422,761 + 16,895,448,638).
- **bootstrap_snapshot_raw (initial_migration):** 811,861,491,702.
- **transfer_unknown_raw (wallet_buy_detected):** 169,099,356,995 (one 16,924,422,761 + one 16,895,448,638 + four 33,819,871,399 — duplicates of tx_exact amounts; cleanup script should mark these duplicate_explained).
- **sold_raw:** 0.
- **sum_active_lots:** 1,014,880,919,096.

### C. Dashboard explanation

- **token_holdings_breakdown:** tx_derived_raw, bootstrap_snapshot_raw, transfer_unknown_raw, sold_raw, sum_active_lots_raw as above. source_category per lot: tx_derived, bootstrap, transfer_unknown.

### D. Reconciliation proof

- **status.json balance_raw (current_balance):** 1,623,722,983,405.
- **sum_active_lots:** 1,014,880,919,096.
- **Mismatch:** current_balance (1,623,722,983,405) ≠ sum_active_lots (1,014,880,919,096). **Gap = 608,842,064,309** (wallet has more than state’s lots; plus duplicate wallet_buy_detected lots inflate transfer_unknown until cleaned).

### E. Trust verdict

**Partially explained.**  
- Two tx_exact lots and one initial_migration explain 845M; remaining active lots include duplicate wallet_buy_detected (same amounts as tx_exact). After DUPLICATE_LOT_CLEANED, transfer_unknown will drop and sum_active_lots will still not equal current_balance; **unexplained gap** 608.8B raw. Root cause: (1) duplicate lots to be marked duplicate_explained; (2) wallet balance higher than sum of lots — additional on-chain activity not yet in state.

---

## Unexplained gaps – root cause and next fix

- **Gaps:** All three tokens show current_balance > sum_active_lots. No fabricated lots; gaps are “wallet holds more than we have lots for.”
- **Root cause:** (1) Legacy wallet_buy_detected created lots from balance deltas (now disabled); (2) some wallet balance increases may not yet have matching parsed txs in state; (3) WAR has duplicate lots for the same tx (one tx_exact + three wallet_buy_detected for same amount).
- **Exact next fix:**  
  - **WAR:** Run a one-off dedupe: for mint WAR, keep one tx_exact lot per tx_signature and mark or merge duplicate wallet_buy_detected lots that match the same tx/delta (e.g. script in `scripts/` or runner logic in `_run_buy_detection` / post-load). File: `mint_ladder_bot/runner.py` or new `scripts/dedupe_war_wallet_buy_lots.py`.  
  - **All:** Ensure tx-first engine and backfill run over sufficient signature window so every balance-increasing tx gets a tx_exact/tx_parsed lot; then re-run reconciliation so current_balance = tx_derived + bootstrap + transfer_unknown − sold (or log STATE_BALANCE_MISMATCH with MINT_HOLDING_EXPLANATION for any remaining gap).
