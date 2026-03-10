# mint-ladder-bot: Market Cap Reference Evaluation

**Strategy — price vs MC as primary.**

---

## Current data

- **status.json** (from status snapshot): Per mint, `market.dexscreener` has `price_native`, `price_usd`, `liquidity_usd`, `volume24h_usd`. No `fdv`, `market_cap`, or `fd_market_cap` in the current schema or DexScreener response usage.
- **state.json:** Entry price (SOL per token), working/original entry. No MC stored.

## Decision

- **Primary reference:** Keep **token price** (SOL per token and USD) as the primary dashboard reference. This is what we have and what the ladder uses (multiples of entry price).
- **Market cap:** Do **not** fabricate. If a data source (e.g. DexScreener API or another provider) later provides **fdv** or **market_cap** in a stable way:
  - Add it to the status/market schema and snapshot.
  - Then the dashboard can show **Entry MC**, **Current MC**, and **MC change %** alongside price.
- **Until then:** MC is optional and not displayed. No invented MC values.

## Rationale

Many traders do track performance vs MC movement. MC is useful when comparable across tokens (e.g. “bought at $500K MC, now $2M”). Without a reliable MC field from the current pipeline, showing it would require either (a) adding it from an API that provides it, or (b) deriving it from price × supply. We do not have supply in the current snapshot; deriving MC would require an extra source and validation. So we keep price as primary and document that MC can be added when the data source supports it.
