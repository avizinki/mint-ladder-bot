# Simulation and multi-wallet validation

This directory holds simulation config and helpers for validating the multi-wallet engine (run-multi) without live execution.

## Config

- **sim_wallets.json** — Defines `wallets` (list of `{ wallet_id, state }`) and `lane_assignments` (list of `{ wallet_id, lane_id, enabled, ... }`). Used when running `run-multi --simulation`.
- **sim_runner.py** — Builds `SimWalletStateSource` from config and reports eligible (wallet_id, lane_id). No execution, no network, no signing.

## Validating with 2+ wallets (T15)

1. **Run with simulation:** From the mint-ladder-bot project root:
   - `run-multi --simulation`
   - Uses `simulation/sim_wallets.json` for wallet states and lane assignments. Use `lane_id: "mint_ladder"` in assignments so eligible lanes match the strategy registry.
2. **Per-wallet state/status:** Place (or create) `state.json` and `status.json` under `{base_dir}/{wallet_id}/` (default base-dir is `.`). For a quick check, minimal files under e.g. `./wallet_a/`, `./wallet_b/` for the wallet_ids in the assignment.
3. **Assert:** Eligible lanes (only active + enabled), guard triggers when STOP file is present at `{base_dir}/STOP` or cwd, and no live tx (dry-run only).
4. Full validation steps and assertions: **docs/trading/mint-ladder-bot-runner-integration-plan.md** §6 (Validation notes) and §6.4 (T15 validation).

No real execution; dry-run/simulation only unless Risk/Manager approve.
