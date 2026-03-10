# TX-First Trust Model

**Principle:** Blockchain transactions are the only source of lot creation. If the chain says one thing and the dashboard/state say another, the model is wrong.

---

## Rule

**BLOCKCHAIN TRANSACTIONS ARE THE ONLY SOURCE OF LOT CREATION.**

- Balance deltas **do not** create trading lots.
- `wallet_buy_detected` **must not** create trading lots.
- Migration/bootstrap lots are isolated and labeled `bootstrap_snapshot`.
- Dashboard distinguishes: **live tx-derived lots** vs **bootstrap holdings** vs **unknown/transfer**.
- Trading bag logic is based on **tx-derived lots only** (unless explicitly configured otherwise).

---

## Balance deltas: allowed uses only

Balance deltas are used only for:

- Reconciliation checks (e.g. `wallet_balance` vs `sum(active_lots)`)
- Alerts and anomaly detection
- "Balance changed but no tx found" warnings

**Allowed log:** `BALANCE_DELTA_WITHOUT_TX`  
**Not allowed:** Creating a new trading lot from balance delta alone.

---

## Lot source taxonomy

| Source | Meaning | Participates in trading bag |
|--------|--------|-----------------------------|
| **tx_exact** | Lot from a single parsed on-chain swap/trade (SOL→token, token→token, etc.) | Yes |
| **tx_parsed** | Lot from parsed tx (e.g. token→token with inferred/unknown cost) | Yes |
| **bootstrap_snapshot** | Migration/snapshot holding; not from a live trade in this run | No (unless confirmed) |
| **transfer_received_unknown** | Received transfer; no swap tx identified | No (or configurable) |
| **initial_migration** | Legacy; treat as bootstrap for display | No |
| **buyback** | Bot buyback | Yes |

---

## Reconciliation model

For each mint we explain holdings as:

```
current_balance =
  tx-derived active lots
  + bootstrap_snapshot lots
  + transfer/unknown received
  - sold amounts
```

Logs:

- **STATE_BALANCE_MISMATCH** when `wallet_balance != sum(explained)` (no fabrication).
- **MINT_HOLDING_EXPLANATION** (or equivalent) for audit: per-mint breakdown by source.

---

## Trust criteria

For each lot the system must be able to answer:

- Where it came from (source + tx_signature if any)
- Whether it was a real swap (tx_exact / tx_parsed)
- Whether it is bootstrap (bootstrap_snapshot)
- Whether price is trustworthy (entry_confidence)
- Whether it participates in trading logic (trading bag inclusion)

If not answerable, the model is incomplete.

---

## References

- Implementation: `CEO_REPORT_REMOVE_BALANCE_DELTA_LOT_CREATION.md`
- Token→token detection: `token-to-token-swap-detection.md`
- Transition: `docs/trading/tx-first-transition-plan.md`
