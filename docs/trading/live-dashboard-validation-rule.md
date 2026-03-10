# Live dashboard validation rule

**Non-negotiable:** A fix is not complete until:

1. The live browser at http://localhost:8765 shows the corrected result.
2. The API response (`GET /runtime/dashboard`) matches it.
3. `state.json` on disk matches it.
4. Only one runtime/dashboard process is active on port 8765.

## Source-of-truth trace

| Layer | Source | How to verify |
|-------|--------|----------------|
| state.json | Disk file in data_dir | `python3 -c "..."` count display-pending |
| API | `build_dashboard_payload(data_dir)` reads state_path = data_dir / "state.json" | `curl http://127.0.0.1:8765/runtime/dashboard` → `pending_lots_count`, count in `recent_buys` |
| Browser | If page is at http://localhost:8765/ it loads state via `fetch('state.json')` (same origin). Server must serve GET /state.json from data_dir. | Refresh page, expand WAR/HACHI/丙午, count rows with confidence "pending_price_resolution" |

## Why the dashboard was still “broken” after file-only validation

1. **Duplicate runtimes:** More than one `mint_ladder_bot.main run` process was running. Each loads state once and periodically saves. A process that had loaded state *before* the downgrade overwrote `state.json` when it saved at end of cycle.
2. **Dashboard not serving state:** The server on 8765 only served GET / (OK) and GET /runtime/dashboard. The “live” dashboard the founder used was likely opening `index.html` as a file and fetching `state.json` from disk (file://). So the table showed whatever was last written to state.json by the running bot(s) — which reverted the downgrade.
3. **Single source of truth:** The dashboard server now serves GET / → index.html, GET /state.json, GET /status.json, GET /run.log from `data_dir`, so opening http://localhost:8765/ uses the same state the API reads.

## Process sanity

- Only one process should bind port 8765.
- If the bot is running, it embeds the dashboard server and uses the same data_dir. The bot’s in-memory state is written to state.json on each cycle; if that state was loaded before a downgrade, it will overwrite the file.
- To validate a downgrade: stop all bot processes, run the downgrade script, then either (a) start only the dashboard server (no bot), or (b) start one bot so it loads the downgraded state and then saves the same.

## Reconciliation overwrite

In `runner.py`, when `sum_lots > actual_raw` the reconciliation path replaces all lots for that mint with a single `resync_lot` with `entry_confidence="snapshot"`. That can reintroduce display-pending for that mint after a downgrade. A permanent fix would require changing reconciliation (e.g. preserve unknown lots or avoid full replace when possible).
