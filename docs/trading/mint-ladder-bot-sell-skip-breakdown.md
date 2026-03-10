# Sell Skip Root Cause

**Owner:** Strategy + Dev + Monitor  
**Date:** 2026-03-08  
**Scope:** Why 45 cycles had 45 skipped sells; explicit SELL_SKIPPED_REASON and summary.

---

## 1. Explicit logging

**SELL_SKIPPED_REASON** is logged for every skipped sell in `_audit_sell` when `action == "skipped"`:

```
SELL_SKIPPED_REASON mint=<mint> symbol=<symbol> step_id=<step_id> reason=<reason>
```

So every skip is grep-able. The full audit line remains:

```
AUDIT sell mint=... action=skipped reason=<reason>
```

---

## 2. Reason mapping (code → directive categories)

| Code reason | Directive category |
|-------------|--------------------|
| invalid_step | (no_sellable_lot_size / invalid) |
| sanity_bag | safe_balance_mismatch |
| liquidity_cap | liquidity_guard |
| dust | dust_threshold |
| slippage_sanity | price_impact_guard / quote |
| price_impact | price_impact_guard |
| quote_stale | quote_unavailable |
| stop_or_rpc_pause | trading_disabled / protection_only |
| duplicate_step | (duplicate) |
| monitor_only | protection_only / trading_disabled |
| balance_mismatch | safe_balance_mismatch |
| exception | (exception) |

Target-not-reached is not an audit-sell reason; the sell is never attempted, so there is no “skipped” audit line for that step. Cycle summary instead has **below_target** (count of steps not attempted because price below target).

---

## 3. Cycle summary (from live logs)

Example cycle line:

```
Cycle N summary: ... below_target=13 ... min_trade_skip=1 ...
```

- **below_target=13:** 13 step evaluations did not reach the sell path because current price was below step target (TARGET_NOT_REACHED).
- **min_trade_skip=1:** 1 step was skipped due to dust (SOL out &lt; min_trade_sol).

So per cycle, most “skips” are **target_not_reached** (logged as TARGET_NOT_REACHED); a smaller number are **dust_threshold** (SELL_BLOCKED_DUST + AUDIT sell reason=dust + SELL_SKIPPED_REASON reason=dust).

---

## 4. 45 cycles, 45 skipped sells — breakdown

- **Interpretation:** Over the run, every cycle had at least one sell evaluation that ended in “skipped” (or only below-target steps and no execution). The **45 skipped** count comes from the audit totals (sells_skipped), i.e. number of times `_audit_sell(..., "skipped", reason)` was called.
- **Primary reasons from logs:**
  - **target_not_reached:** Most steps never get to quote/execute because price is below step target; these are logged as TARGET_NOT_REACHED, not as AUDIT sell skipped. So the 45 skipped are the subset that *did* reach the sell path but were then skipped (dust, liquidity, quote_stale, etc.).
  - **dust_threshold:** At least one mint (e.g. “Dust”) repeatedly skipped: “Mint Dust step_id=1 skipped: dust (0.000064 SOL &lt; min_trade_sol 0.001000)” and AUDIT sell reason=dust.
- **Count per reason:** Run `grep SELL_SKIPPED_REASON run.log` (or archived run log) and aggregate by `reason=`. Example:
  - `reason=dust` → dust_threshold
  - `reason=liquidity_cap` → liquidity_guard
  - `reason=monitor_only` → protection_only / trading_disabled
  - etc.

---

## 5. Summary table (from code and logs)

| Reason (directive) | Source in code/logs |
|-------------------|---------------------|
| target_not_reached | TARGET_NOT_REACHED; cycle summary below_target |
| dust_threshold | SELL_BLOCKED_DUST, AUDIT reason=dust, SELL_SKIPPED_REASON reason=dust |
| liquidity_guard | reason=liquidity_cap |
| quote_unavailable | reason=quote_stale |
| price_impact_guard | reason=price_impact, reason=slippage_sanity |
| cooldown_guard | (cooldown checked earlier; can surface as other skip) |
| protection_only | reason=monitor_only |
| trading_disabled | reason=stop_or_rpc_pause |
| safe_balance_mismatch | reason=balance_mismatch, reason=sanity_bag |
| no_sellable_lot_size | reason=invalid_step (e.g. child_amount <= 0) |

For the run with 45 skipped: the majority of *attempted* sell evaluations that were skipped were **dust_threshold** (one small mint below min_trade_sol); the rest of “no sells” is explained by **target_not_reached** (price below target, so no sell path entered).
