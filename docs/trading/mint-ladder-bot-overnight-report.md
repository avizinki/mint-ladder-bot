# mint-ladder-bot: Overnight Report

**Fill this in the morning after unattended run.**  
Use run.log, events.jsonl, state.json, and health_status.json to populate.

---

## 1. Runtime start time

- **Started at:** 2026-03-07 21:52 UTC (full reset)
- **Archive used:** archive/full_reset_20260307_2352/
- **Mode:** LIVE (not monitor-only)

---

## 2. Total cycles executed

- **Count:** _from run.log "Cycle N summary" lines; last N = total_
- **Approx. duration:** _first cycle timestamp to last cycle timestamp_

---

## 3. Tokens detected overnight

- **New mints discovered:** _WALLET_MINT_DISCOVERED / MINT_DETECTED in run.log or events.jsonl_
- **List (mint / symbol):** _brief list_

---

## 4. Lots created

- **Count:** _LOT_CREATED or LOT_ADDED / BUY_DETECTED in run.log_
- **By mint:** _optional short list_

---

## 5. Sell attempts or executions

- **Sell attempts (AUDIT sell / step hit):** _count or list_
- **Sells executed (sells_ok in cycle summary):** _total_
- **Sells failed (sells_fail):** _total_
- **Notable:** _any txs, signatures, or mints_

---

## 6. Alerts or warnings

- **STOP_LOSS_TRIGGERED:** _count / mints_
- **RECONCILIATION_WARNING:** _count_
- **LIQUIDITY_COLLAPSE / Mint skipped liquidity:** _count_
- **SELL_ABORT_BALANCE_MISMATCH:** _count_
- **Circuit breaker / global pause:** _yes/no_
- **Other:** _cooldown, dust skip, etc._

---

## 7. Restarts (if any)

- **Restart count:** _
- **Reason(s):** _e.g. health check failed, crash, watchdog_
- **Log reference:** _run.log or monitor log lines_

---

## 8. RPC errors or network issues

- **429 / rate limit:** _count_
- **Timeout / connection:** _count_
- **Other RPC errors:** _brief_

---

## 9. Anything unusual

- _Free-form: unexpected mints, balance drift, long pauses, repeated failures, etc._

---

## 10. PnL summary (if available)

- **Realized (sells executed):** _from state / events_
- **Unrealized (mark-to-market):** _from state.json position value vs cost_
- **Buybacks triggered / spent:** _if BUYBACK_ENABLED_

---

*Report generated from run.log and runtime artifacts. Update the sections above after the overnight run.*

**When done:** Notify founder: `./tools/notify_founder.sh "Overnight report ready"` (see docs/trading/mint-ladder-bot-notifications.md).

---

## Monitoring during run

- **run.log** — Cycle summaries, WALLET_MINT_DISCOVERED, BUY_DETECTED, LOT_ADDED, sell AUDIT, STOP_LOSS_TRIGGERED, RECONCILIATION_WARNING, liquidity skips, RPC failures, cooldowns.
- **events.jsonl** — MINT_DETECTED, LOT_CREATED, SELL_SENT, SELL_CONFIRMED, SELL_FAILED, RECONCILED, CIRCUIT_BREAKER (machine-readable).
- **Health / watchdog (optional):** `tools/check_runtime_health.py --status-file health_status.json` (bot process, log progress, RPC); `tools/watchdog.py` for periodic health and restart; port 8765 if running `python -m http.server 8765` for dashboard.
- **On failure:** Run health check; attempt restart (e.g. `tools/restart_runtime.sh` or re-run `run`); log reason; escalate after repeated failures.
