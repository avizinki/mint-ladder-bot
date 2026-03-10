# TX-First Trust Model: Transition Plan

**Goal:** Move from mixed truth sources (tx_exact + wallet_buy_detected + initial_migration) to a tx-first-only trust model without destroying useful data.

---

## 1. Legacy lot types deprecated

| Legacy source | Status | Action |
|---------------|--------|--------|
| **wallet_buy_detected** | Deprecated for **creation** | No new lots. Existing lots in state remain; display as transfer_unknown or bootstrap for breakdown. |
| **initial_migration** | Renamed / mapped | Treated as **bootstrap_snapshot** in trading bag logic and dashboard (excluded from sellable). |
| **snapshot** | Renamed / mapped | Same as bootstrap_snapshot for display and bag. |

**Still in use:**

- **tx_exact** — Lot from single parsed on-chain swap.
- **tx_parsed** — Lot from parsed tx (e.g. token→token with inferred/unknown cost).
- **bootstrap_snapshot** — Migration/snapshot; excluded from trading bag unless confirmed.
- **transfer_received_unknown** — For future use (e.g. transfer-in with no swap).
- **buyback** — Bot buyback.

---

## 2. How current state is migrated or isolated

- **No automatic rewrite of existing state.** Existing lots keep their `source` (e.g. initial_migration, wallet_buy_detected). Dashboard and reconciliation **interpret** them:
  - `initial_migration` / `snapshot` → show as **bootstrap**; excluded from trading bag.
  - `wallet_buy_detected` → show as **transfer_unknown** in breakdown; excluded from trading bag (already excluded because not tx_exact/tx_parsed).
- **New lots** are created only from:
  - Tx-first engine (tx_exact / tx_parsed),
  - Bootstrap/migration path (bootstrap_snapshot),
  - Tx lookup in _run_buy_detection (tx_exact only).
- Optional **one-off migration script**: relabel `initial_migration` → `bootstrap_snapshot` in state for consistency. Not required for correctness; dashboard already treats them the same.

---

## 3. Fresh clean tx-first run vs migrate

- **Preferred for full trust reset:** A **fresh clean tx-first run** (see `workforce/playbooks/real-clean-run-playbook.md` and `tools/real_clean_run.sh`):
  - Archive current state/status/run.log.
  - Do **not** merge archived records back.
  - Start with `CLEAN_START=1`; state is built from status with **no historical lots**.
  - Tx-first engine and tx lookup create lots only from parsed txs as new activity occurs.
- **Alternative:** Keep current state and **stop creating** balance-delta lots. Existing mixed lots remain; dashboard and reconciliation show breakdown (tx_derived vs bootstrap vs transfer_unknown). Over time, new activity is tx-only.

---

## 4. Preserve audit evidence while cleaning active runtime state

- **Archive before clean run:** Move state.json, status.json, run.log, and related artifacts to `archive/real_clean_run_<timestamp>/`. Do **not** delete; preserve for audit and comparison with Jupiter/wallet history.
- **Logs:** Keep BALANCE_DELTA_WITHOUT_TX, STATE_BALANCE_MISMATCH, MINT_HOLDING_EXPLANATION in run.log and event journal so “why do I see this balance?” is answerable.
- **Dashboard:** `token_holdings_breakdown` and per-lot `source_category` allow comparing dashboard to wallet/Jupiter without guessing.

---

## Summary

| Question | Answer |
|----------|--------|
| Legacy types deprecated? | wallet_buy_detected (no new creation); initial_migration/snapshot treated as bootstrap_snapshot. |
| Current state migrated? | Interpreted, not blindly overwritten; optional relabel script. |
| Fresh run preferred? | Yes for full trust reset; playbook and script provided. |
| Audit preserved? | Archive before clean; logs and breakdown explain holdings. |
