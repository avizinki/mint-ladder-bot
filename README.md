## mint-ladder-bot

A **console-first** Solana meme-coin ladder trading bot: snapshot your wallet, plan 20-step profit-taking ladders per token, and run live sells (and optional buy-backs) via Jupiter. Includes a **static dashboard** (`index.html`) that auto-refreshes from `state.json`, `status.json`, and `run.log`.

- **status** — One-time wallet snapshot → `status.json` (SOL + SPL balances, DexScreener + Jupiter metadata, best-effort entry inference).
- **plan** — Print ladder steps per mint from `status.json` (optionally show executed steps from `state.json`). No trades.
- **run** — Live loop: refresh balances, evaluate ladders, execute sells (and optional buy-backs) via Jupiter; persist state to `state.json` and logs to `run.log`.

**Requirements:** Python 3.10+, Solana RPC, Jupiter API (optional API key from [portal.jup.ag](https://portal.jup.ag)). Use a **burner wallet** for testing.

### Quick start

1. **Create and activate a virtualenv**

```bash
python -m venv .venv
source .venv/bin/activate
```

2. **Install in editable mode**

```bash
pip install -e .
```

3. **Take a wallet snapshot**

```bash
mint-ladder-bot status --wallet <PUBKEY> --out status.json
```

(Without installing the CLI: `python -m mint_ladder_bot status ...`)

4. **Review and edit entry prices**

Open `status.json` and, for any mint where `entry.entry_price_sol_per_token` is `0`, manually fill in a reasonable entry price in SOL per token. You may also edit `entry_source` to `"user"` to indicate manual input.

5. **Export your private key (burner wallet recommended)**

```bash
export PRIVATE_KEY_BASE58=...
```

> **Important:** Always use a **burner wallet first**. Never paste a high-value key into environment variables or terminals. The bot never logs or writes your private key, but your shell history and OS still might.

6. **Run the live ladder bot**

```bash
mint-ladder-bot run --status status.json --state state.json
```

This creates/updates `state.json` and appends to `run.log` in the project directory. Stop with `Ctrl+C`; state is saved on exit.

7. **Dashboard (optional)**  
   Open `index.html` in a browser (e.g. serve the repo folder with `npx serve .`). It reads `state.json`, `status.json`, and `run.log` and auto-refreshes every 15 seconds. Ensure the bot writes these files in the same directory as `index.html`.

### Commands

- **Status**

  ```bash
  mint-ladder-bot status --wallet <PUBKEY> --out ./status.json
  ```

  - Fetches SOL and SPL token balances for the wallet.
  - Enriches tokens with best-effort metadata and DexScreener market data.
  - Attempts to infer an entry price per mint from recent swap transactions.
  - Writes a single snapshot file `status.json` and exits.

- **Plan**

  ```bash
  mint-ladder-bot plan --status ./status.json
  ```

  - Loads `status.json` and computes a 20-step ladder per mint.
  - Prints target prices and sell sizes for each step without placing trades.

- **Run**

  ```bash
  mint-ladder-bot run --status ./status.json [--monitor-only]
  ```

  - Requires `PRIVATE_KEY_BASE58` in the environment (unless using `--monitor-only` for a dry run).
  - Uses `status.json` as the source of mints, balances, and entry prices.
  - Maintains a `state.json` file with runtime state and executed steps.
  - Trades via Jupiter swaps in a round-robin loop until killed.
  - With `--monitor-only`: runs the full loop but never sends swaps; builds ladders, fetches quotes, applies caps, and writes audit logs with `reason=monitor_only`.

### Safety notes

- **Use a burner wallet first.** Verify behavior with small amounts before risking meaningful capital.
- **Low-liquidity coins can slip.** DexScreener liquidity estimates are best-effort; thin markets can experience significant price impact and slippage.
- **Pausing a mint.** You can pause trading for a specific mint by editing `state.json` and setting its `paused_until` field (inside the `failures` object) to an ISO8601 timestamp in the future. The runner will skip that mint until the time has passed.

### Buy-back (optional)

When **buy-back is enabled** (`BUYBACK_ENABLED=true`), the bot will use SOL to buy token when price drops below entry by a set percentage (e.g. 10% with `BUYBACK_TRIGGER_PCT=0.10`). Spending is capped per trade and per mint (see `.env.example`). A reserve (`BUYBACK_SOL_RESERVE`) is never spent so the wallet keeps enough for fees. Buy-back state (total SOL spent per mint) is stored in `state.json` under each mint’s `buybacks` field.

### Configuration

Copy `.env.example` to `.env` and adjust. Most settings have sane defaults; key overrides: `RPC_ENDPOINT`, `MIN_TRADE_SOL`, `JUPITER_API_KEY` (optional), and buy-back variables. **Do not commit `.env`** — it is gitignored. `status.json`, `state.json`, and `run.log` are also gitignored (local/runtime only).

### Live rollout procedure

Before risking real capital, follow this sequence:

1. **Generate status** — Run `mint-ladder-bot status --wallet <PUBKEY> --out status.json` to snapshot your wallet and mints.
2. **Verify entries manually** — Open `status.json` and ensure every mint you intend to trade has a valid `entry.entry_price_sol_per_token` (and `entry_source` set to `"user"` if you edited it). Mints with zero or unknown entry get **market bootstrap** at run startup: the bot reads the current DexScreener price from `status.json` (`market.dexscreener.price_native`). If that price is present and valid, it sets the runtime entry in `state.json` only (with `entry_source = "market_bootstrap"`); `status.json` is not overwritten and no buy is executed. If the market price is missing or zero, the mint stays non-tradable (logged as "Mint skipped: market price unavailable"). Alternatively you can use **bootstrap buy** (one tiny SOL→token buy, `BOOTSTRAP_BUY_SOL`) to set entry from an actual trade; that is independent of market bootstrap.
3. **Start with monitor-only** — Run `mint-ladder-bot run --status status.json --monitor-only` for at least one full cycle. The bot will build ladders, fetch quotes, apply cooldown/caps, and write audit logs with `action=skipped reason=monitor_only`; no swaps are sent.
4. **Switch to a burner wallet** — Use a dedicated wallet with only the capital you are willing to lose. Set `PRIVATE_KEY_BASE58` for that wallet (e.g. in `.env` or export).
5. **Start with tiny capital** — Fund the burner with minimal SOL and a small amount of the tokens you want to ladder. Confirm `MIN_TRADE_SOL` and liquidity caps are appropriate.
6. **Observe audit logs** — Watch `run.log` for `AUDIT sell` lines (mint, step_id, action, reason). Ensure skips and executions match your expectations before scaling.
7. **Only then consider larger sizing** — After you are satisfied with behavior in monitor-only and with a small live run, you may increase size or add more mints. Keep using a burner until you are fully confident.

### License

MIT. See [LICENSE](LICENSE).

