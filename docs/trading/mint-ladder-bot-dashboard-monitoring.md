# mint-ladder-bot: Dashboard Monitoring Events

**Monitor — events and refresh.**

---

## Events to log (runtime)

| Event | When |
|-------|------|
| **BUY_DETECTED** | When buy detection creates or updates a lot (runner already logs this). |
| **LOT_CREATED** | When a new lot is created (runner appends to event journal). |
| **MC_UPDATED** | When market cap (or price used for MC) is refreshed — optional; only if MC is added to data source later. |
| **DASHBOARD_REFRESH** | Dashboard does not emit this; it refreshes on browser reload or when the page reloads state/status/log (e.g. every 15s if auto-refresh is implemented). |

## Dashboard refresh behavior

- **Current:** Dashboard loads `state.json`, `status.json`, `run.log` once on load. No automatic polling in the provided snippet; `REFRESH_MS = 15000` exists but must be wired to a periodic reload of the three assets to get “refresh when new buy / cycle completes.”
- **Desired:** When the page periodically refetches state/status (and optionally events.jsonl), then:
  - New buy → new lot in state → next fetch shows it in Recent Buys.
  - Lot created → state updated → next fetch shows it.
  - Price/MC changes → status or state updated → next fetch updates table.
  - Runtime cycle completes → state/log updated → next fetch shows new cycle stats.

To get true “refresh when new buy detected,” add a timer (e.g. every REFRESH_MS) that re-requests state.json, status.json, and run.log and re-runs the same load/merge/render path (including `buildRecentBuys` and `renderRecentBuys`). No new event types are required for the dashboard itself; BUY_DETECTED and LOT_CREATED are already logged by the bot.
