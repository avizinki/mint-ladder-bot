# mint-ladder-bot: Dashboard Number Formatting

**Engineering — smart K/M/B formatting.**

---

## Requirement

All large numeric values use human-friendly formatting so numbers are easier to read for trading decisions.

## Rules

- **1,200** → 1.2K  
- **15,500** → 15.5K  
- **2,400,000** → 2.4M  
- **1,300,000,000** → 1.3B  

Precision is preserved internally (state/status unchanged); only display is abbreviated.

## Scope

Formatting applied to:

| Field | When | Example |
|-------|------|--------|
| Wallet SOL balance | When \|value\| ≥ 1000 | 1.2K SOL |
| On-book value (portfolio) | When \|value\| ≥ 1000 | 2.4M SOL |
| Token balance (bag) | When \|value\| ≥ 1000 | 15.5K |
| Liquidity USD | All | $957K, $1.2M |
| Position value SOL | When \|value\| ≥ 1 | 0.5K SOL or 1.2M SOL |
| Trading bag UI | All | formatCompact |
| Moonbag UI | All | formatCompact |

Small values (e.g. SOL &lt; 1000) keep full decimal display (e.g. 0.187 SOL) via existing `fmtSol`.

## Implementation

- **formatCompact(value)** in dashboard JS: returns "–" for null/NaN; for \|n\| ≥ 1e9 → X.XB; ≥ 1e6 → X.XM; ≥ 1e3 → X.XK; else toLocaleString with max 4 fraction digits.
- Applied in `renderSummary` (wallet SOL, on-book value) and `renderTable` (balance, position value, trading bag, moonbag, liquidity_usd).
