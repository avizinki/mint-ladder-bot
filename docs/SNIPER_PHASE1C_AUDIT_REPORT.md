## Sniper Phase 1C Audit Report

This document audits the **current** sniper-related implementation in `mint-ladder-bot` as of Phase 1C (manual-seed candidate pipeline). It is based on the actual source code, wiring, and tests, not the plan.

---

## 1. Implementation inventory

This section lists the key sniper-related files, their purpose, current status (active vs scaffold), and important functions/classes.

### 1.1 `mint_ladder_bot/models.py`

- **Purpose**: Core Pydantic models for runtime state, mints, lots, and newly-added sniper state.
- **Key sniper elements**:
  - `SniperManualSeedQueueEntry`
    - Fields: `mint`, `enqueued_at`, `source="manual_seed"`, `note`.
  - `SniperAttemptState` (`Literal[...]`)
  - `SniperDecisionOutcome` (`Literal[...]`)
  - `SniperCooldownEntry`
  - `SniperDecisionEntry`
  - `SniperAttempt`
  - `SniperStats`
  - `RuntimeState` sniper fields:
    - `sniper_pending_attempts: Dict[str, SniperAttempt]`
    - `sniper_attempt_history: List[SniperAttempt]`
    - `sniper_last_decisions: List[SniperDecisionEntry]`
    - `sniper_candidate_cooldowns: Dict[str, SniperCooldownEntry]`
    - `sniper_recent_success_timestamps_hour: List[int]`
    - `sniper_recent_success_timestamps_day: List[int]`
    - `sniper_manual_seed_queue: List[SniperManualSeedQueueEntry]`
    - `sniper_stats: SniperStats`
    - `processed_sniper_signatures: List[str]`
- **Status**: **Active** (loaded on every `state.json` read; used by service/tests).

### 1.2 `mint_ladder_bot/events.py`

- **Purpose**: Central definitions for event names and JSONL event persistence.
- **Key sniper elements**:
  - Event constants:
    - `SNIPER_CANDIDATE_DISCOVERED`
    - `SNIPER_CANDIDATE_NORMALIZED`
    - `SNIPER_CANDIDATE_REJECTED`
    - `SNIPER_CANDIDATE_SCORED`
    - `SNIPER_BUY_REQUESTED` (future)
    - `SNIPER_BUY_QUOTE_ACCEPTED` (future)
    - `SNIPER_BUY_QUOTE_REJECTED` (future)
    - `SNIPER_BUY_SUBMITTED` (future)
    - `SNIPER_BUY_OBSERVED` (future)
    - `SNIPER_BUY_CONFIRMED` (future)
    - `SNIPER_BUY_FAILED` (future)
    - `SNIPER_BUY_UNCERTAIN` (future)
    - `SNIPER_PENDING_RESOLVED` (future)
    - `SNIPER_LOT_ARMED` (future)
    - `SNIPER_DUPLICATE_BLOCKED`
    - `SNIPER_COOLDOWN_BLOCKED`
    - `SNIPER_RISK_BLOCKED`
  - `append_event` / `read_events` used across the runtime.
- **Status**:
  - Sniper candidate and block events: **Defined, but not yet wired** into code paths (no current references).
  - Execution-related sniper events: **Defined for future use**, currently unused.

### 1.3 `mint_ladder_bot/dashboard_server.py`

- **Purpose**: Build JSON payload for `/runtime/dashboard` and operator dashboard.
- **Sniper-related logic**:
  - `_build_sniper_summary(state: Dict[str, Any] | None)`:
    - Derives:
      - `manual_seed_queue_size` from `state["sniper_manual_seed_queue"]` length (if present).
      - `pending_attempts_count` from `state["sniper_pending_attempts"]` (currently always `{}`).
      - `open_sniper_positions_count` from `state["sniper_stats"]["open_sniper_positions_count"]` (field not yet populated anywhere).
      - `recent_success_count_1h/24h` from `sniper_recent_success_timestamps_*` (currently unused).
      - `last_decision_at` from `sniper_last_decisions[].ts`.
      - `last_buy_at` from `sniper_stats["last_buy_at"]` (not populated yet).
    - Hard-codes:
      - `"enabled": False`
      - `"mode": "disabled"`
      - `"discovery_enabled": False`
  - `build_dashboard_payload(...)`:
    - Calls `_build_sniper_summary(...)`.
    - Adds `sniper_summary`, `sniper_pending_attempts`, `sniper_recent_decisions` keys to payload, but:
      - `sniper_pending_attempts` is always `[]` (placeholder).
      - `sniper_recent_decisions` is always `[]` (placeholder).
- **Status**:
  - Summary structure: **Active** (always present in dashboard payload).
  - `sniper_recent_decisions` content: **Scaffold only** (not wired to `state.sniper_last_decisions` yet).
  - `enabled/mode/discovery_enabled` flags: **Hard-coded**, not sourced from config.

### 1.4 `mint_ladder_bot/runner.py`

- **Purpose**: Core runtime loop (`run_bot`) that drives:
  - status refresh
  - tx-first lot engine
  - ladder
  - backfill
  - reconciliation
  - etc.
- **Sniper-related logic**:
  - `SniperService` import:
    - `from .sniper_engine.service import SniperService`
  - In `run_bot(...)`:
    - After loading state/status:
      - `state: RuntimeState = load_state(state_path, status_path)`
      - `sniper_service = SniperService(config=config, state=state)`
    - Inside main cycle loop:
      - Legacy `_run_sniper_cycle(...)` remains defined but is **no longer called**.
      - New hooks:
        ```python
        # Sniper Phase 1: resolve pending attempts and process manual-seed queue.
        try:
            sniper_service.resolve_pending_attempts()
        except Exception as e:
            logger.warning("Sniper resolve_pending_attempts failed: %s", e)
        try:
            sniper_service.process_candidate_queue()
        except Exception as e:
            logger.warning("Sniper process_candidate_queue failed: %s", e)
        ```
- **Status**:
  - `_run_sniper_cycle(...)`: **Scaffold / legacy**, fully unused in the new flow.
  - `SniperService` instantiation & hook calls: **Active**, but `resolve_pending_attempts` and `process_candidate_queue` are currently **no-op even when enabled** (based on current `service.py`).

### 1.5 `mint_ladder_bot/main.py`

- **Purpose**: CLI entrypoints using Typer.
- **Sniper-related logic**:
  - `sniper-enqueue` command:
    - Parses `--mint` and optional `--note`.
    - Loads `.env`, `Config`, `state.json`, `status.json`.
    - Constructs `SniperService(config, state)`.
    - Calls `enqueue_manual_seed`.
    - If accepted → `save_state_atomic`.
    - Prints `ACCEPTED queue_size=N` or `REJECTED reason=<reason> queue_size=N`.
- **Status**: **Active**, tested in `test_sniper_service` indirectly (service behavior), but CLI command itself is not directly unit-tested.

### 1.6 `mint_ladder_bot/sniper_engine/runtime.py`

- **Purpose**: Low-level helpers for sniper runtime state manipulation.
- **Functions**:
  - State classification:
    - `PENDING_STATES`, `TERMINAL_STATES`
    - `is_pending_sniper_attempt_state`, `is_terminal_sniper_attempt_state`
  - Queue helpers:
    - `queue_contains_mint`
    - `pending_attempt_exists_for_mint`
    - `open_lot_exists_for_mint`
    - `mint_is_blocked_for_enqueue`
    - `enqueue_manual_seed` (appends `SniperManualSeedQueueEntry`)
    - `dequeue_next_manual_seed_batch`
  - Attempt helpers:
    - `add_pending_attempt`
    - `move_attempt_to_history`
    - `remove_pending_attempt`
- **Status**:
  - Queue helpers: **Active** (used by `SniperService.enqueue_manual_seed`).
  - Attempt helpers: **Scaffold**, not yet called by any code (no attempts created yet).

### 1.7 `mint_ladder_bot/sniper_engine/service.py`

- **Purpose**: Thin control-plane layer over sniper runtime state.
- **Key methods**:
  - `mode`, `is_enabled`, `is_live_mode`, `is_paper_mode`.
  - `enqueue_manual_seed`:
    - Enforces disabled mode, mint presence, queue size, duplicate, pending attempt, open lot.
    - Delegates to `runtime.enqueue_manual_seed`.
  - `dequeue_next_manual_seed_batch`:
    - Delegates to runtime helper if enabled.
  - `resolve_pending_attempts`:
    - Currently **no-op** regardless of mode, returns immediately.
  - `process_candidate_queue`:
    - Currently **no-op** regardless of mode, returns immediately.
- **Status**:
  - Queue operations: **Active** via CLI and tests.
  - Cycle hooks: **Placeholders only**; no normalization, risk, or scoring present in actual code yet.

### 1.8 `mint_ladder_bot/sniper_engine/candidate.py`

- **Status**: **Does not exist** in the repository.
- All references to `SniperCandidate` are in docs only; no such class or helper currently in code.

### 1.9 `mint_ladder_bot/sniper_engine/normalize.py`

- **Status**: **Does not exist**.
- No normalization layer is currently implemented; no `normalize_candidate` logic is present.

### 1.10 `mint_ladder_bot/sniper_engine/risk.py`

- **Status**: **Does not exist**.
- No sniper-specific risk module is implemented; risk logic remains implicit in legacy sniper path (unused) and global risk engine.

### 1.11 `mint_ladder_bot/sniper_engine/scoring.py`

- **Status**: **Does not exist**.
- No scoring model exists in current code; only general ladder/strategy scoring exists elsewhere.

### 1.12 `tests/test_sniper_baseline.py`

- **Purpose**: Baseline compatibility tests for sniper fields and dashboard.
- **Tests**:
  - `test_runtime_state_backward_compat_without_sniper_fields`:
    - Asserts `RuntimeState` can be created from old-style JSON and that sniper fields default correctly.
  - `test_runtime_state_round_trip_with_sniper_defaults`:
    - Ensures `save_state_atomic` + `load_state` preserve sniper defaults.
  - `test_dashboard_payload_includes_sniper_sections_when_empty`:
    - Ensures dashboard payload always contains `sniper_summary`, `sniper_pending_attempts`, `sniper_recent_decisions`.
- **Status**: **Active**, passing.

### 1.13 `tests/test_sniper_service.py`

- **Purpose**: Tests for `SniperService` queue behavior and mode gating.
- **Tests**:
  - `test_sniper_service_modes_disabled_by_default`
  - `test_enqueue_rejected_when_disabled`
  - `test_enqueue_accepts_valid_when_enabled`
  - `test_enqueue_rejects_duplicate_in_queue`
  - `test_enqueue_rejects_queue_full`
  - `test_enqueue_rejects_when_open_lot_exists`
- **Status**: **Active**, passing.
- **Note**: No tests for `resolve_pending_attempts` or `process_candidate_queue` yet, consistent with their no-op state.

### 1.14 `tests/test_sniper_candidate_pipeline.py`

- **Status**: **Does not exist**.
- Any description previously given for it in conversation is speculative; no such file is present in the repo.

---

## 2. Spec compliance review

This section compares code against the approved Phase 1 spec (up through the start of Phase 1C).

### 2.1 RuntimeState additions

- **Code**: `RuntimeState` includes all sniper fields described in the spec with appropriate defaults.
- **Status**: **Compliant**.

### 2.2 Structured manual-seed queue

- **Code**: `RuntimeState.sniper_manual_seed_queue: List[SniperManualSeedQueueEntry]`.
- `SniperManualSeedQueueEntry` matches the spec (mint, enqueued_at, source, note).
- `SniperService.enqueue_manual_seed` and `runtime.enqueue_manual_seed` use these.
- **Status**: **Compliant and active**.

### 2.3 SniperService

- **Code**: Exists with mode gating and queue operations; cycle hooks are stubbed.
- **Spec vs. implementation**:
  - Mode and `is_enabled` behavior: **Compliant**.
  - Queue operations and reason codes: **Compliant**.
  - `resolve_pending_attempts` and `process_candidate_queue`:
    - Spec: should eventually perform reconciliation and candidate processing.
    - Current code: **No-op** placeholders.
- **Status**: **Partially compliant** — service shell exists; candidate pipeline not yet implemented.

### 2.4 Runner hook placement

- **Code**:
  - `SniperService` constructed after `load_state`.
  - Inside the cycle loop, `_run_sniper_cycle` is replaced by calls to `resolve_pending_attempts` and `process_candidate_queue`.
- **Spec**: Hooks should be present and config-gated, and must not alter behavior when disabled.
- **Status**:
  - Hook placement: **Compliant** (in the intended “sniper slot” between observation and ladder).
  - Behavior when disabled: **Compliant** (strict no-op).
  - Behavior when enabled: **Currently no-op**; spec for Phase 1C expects candidate processing, but that has **not** been implemented yet.

### 2.5 CLI enqueue path

- **Code**: `sniper-enqueue` command in `main.py` matches the spec: uses SniperService, persists on accept, prints deterministic messages.
- **Status**: **Compliant and active**.

### 2.6 Candidate model

- **Spec**: `SniperCandidate` model with rich fields (liquidity, volume, stage, risk flags, score, etc.).
- **Code**: No `SniperCandidate` type exists; candidates do not exist as first-class objects.
- **Status**: **Not implemented**.

### 2.7 Normalization

- **Spec**: `sniper_engine/normalize.py` that fills candidate fields from existing data, emitting `SNIPER_CANDIDATE_NORMALIZED`.
- **Code**: No such module; no normalization functions; no references to `SNIPER_CANDIDATE_NORMALIZED` in code.
- **Status**: **Not implemented**.

### 2.8 Risk filter

- **Spec**: Deterministic, config-driven risk filters with clear reason codes and events.
- **Code**:
  - Queue-level duplicate/open-lot/pending rules enforced in `SniperService.enqueue_manual_seed` and `runtime` helpers.
  - No candidate-level risk filtering (liquidity, stage, caps, cooldown based on candidate properties).
  - No `sniper_engine/risk.py`.
- **Status**: **Partially compliant**:
  - Queue-level blocking exists.
  - Full Phase 1 risk layer is **not implemented**.

### 2.9 Scoring

- **Spec**: Deterministic scoring model with liquidity/spread/volume/activity factors; `SNIPER_CANDIDATE_SCORED` events.
- **Code**: No sniper scoring functions; `SNIPER_CANDIDATE_SCORED` is not referenced.
- **Status**: **Not implemented**.

### 2.10 Decision recording

- **Spec**: `SniperDecisionEntry` history updated with exact outcome enums.
- **Code**:
  - `SniperDecisionEntry` exists, but:
    - `state.sniper_last_decisions` is never updated anywhere.
  - Decision outcomes are not recorded for enqueue outcomes or hypothetical candidates.
- **Status**: **Not implemented** for candidates; partly present conceptually in data model.

### 2.11 Event emission

- **Spec**: Candidate pipeline events (`SNIPER_CANDIDATE_*`, `SNIPER_*_BLOCKED`) should be emitted.
- **Code**:
  - Events are **defined** but not used; there is no sniper-specific `append_event(...)` call in current code.
- **Status**: **Not implemented** (no runtime emission yet).

### 2.12 Dashboard recent decisions

- **Spec**: `sniper_recent_decisions` should display recent candidate decisions.
- **Code**:
  - `build_dashboard_payload` returns `sniper_recent_decisions: []` unconditionally.
  - No reading from `state.sniper_last_decisions`.
- **Status**: **Scaffold only**, not wired.

### 2.13 Disabled-mode no-op behavior

- **Code**:
  - `SniperService.is_enabled()` uses `sniper_enabled` and `sniper_mode`.
  - All cycle hooks and queue batch processing are short-circuited via `is_enabled` checks (hooks are no-op).
  - Tests explicitly verify disabled enqueue behavior.
- **Status**: **Compliant**.

### 2.14 No-attempts-created guarantee

- **Code**:
  - No code currently creates `SniperAttempt` instances.
  - `sniper_pending_attempts` is written only via helpers that are not called anywhere.
- **Status**: **Compliant** — no attempts created yet.

### 2.15 No-lot / no-ladder-changes guarantee

- **Code**:
  - All sniper code touches only sniper-specific state fields and does not call `LotInfo.create`, `LADDER_ARMED`, or any ladder methods.
  - Legacy `_run_sniper_cycle` (with lot creation) is unused.
- **Status**: **Compliant** — sniper has zero impact on lots and ladder at this stage.

---

## 3. Active runtime flow proof

This section describes what actually happens at runtime in three scenarios, based on `runner.py`, `SniperService`, and tests.

### 3.1 Sniper disabled

Conditions:

- `Config().sniper_enabled` is `False` by default.
- `Config().sniper_mode` defaults to `"disabled"`.

Flow:

1. `run_bot` loads `state` and `status`.
2. `SniperService(config, state)` is constructed.
3. In each cycle:
   - `sniper_service.resolve_pending_attempts()`:
     - `is_enabled()` returns `False`.
     - Method returns immediately, no state changes.
   - `sniper_service.process_candidate_queue()`:
     - Same: `is_enabled()` is `False`, early return.
4. Ladder, tx-first, and execution paths proceed exactly as before, unaffected by sniper.

What does **not** happen:

- No candidate discovery.
- No normalization.
- No sniper-specific risk or scoring.
- No `SniperAttempt` objects are created.
- No sniper events emitted.
- No lot or ladder changes via sniper.

### 3.2 Sniper enabled, queue empty

Conditions:

- `config.sniper_enabled = True`, `config.sniper_mode = "live"` or `"paper"`.
- `state.sniper_manual_seed_queue == []`.

Flow:

1. `SniperService.is_enabled()` returns `True`.
2. `resolve_pending_attempts()`:
   - Still no-op (no code implemented inside).
3. `process_candidate_queue()`:
   - No implementation → returns immediately without dequeuing or processing.
4. No changes to state or events.

What does **not** happen:

- No candidate processing (because function body is still placeholder).
- Same guarantees as disabled mode for lots, ladder, and attempts.

### 3.3 Sniper enabled, queue has one manual-seed mint

Conditions:

- `sniper_enabled=True`, `sniper_mode="live"`.
- One call to `sniper-enqueue` or direct service call:
  - `enqueue_manual_seed("MintA")` accepted → one `SniperManualSeedQueueEntry(mint="MintA", ...)` in state.

Flow:

1. Next `run_bot` cycle constructs `SniperService(config, state)` with queue non-empty.
2. `resolve_pending_attempts()`:
   - Still no-op.
3. `process_candidate_queue()`:
   - Still a no-op placeholder in current code; does **not** call any candidate/normalize/risk/scoring functions.
   - Queue remains unchanged.

What does **not** happen:

- No dequeueing of queue entries.
- No candidate objects.
- No normalization, risk, or scoring.
- No decision history or stats updated.
- No changes to lots, attempts, or ladder.

Conclusion: **as of now, Phase 1C candidate pipeline is not yet implemented in code**; the system only supports enqueueing manual-seed entries and leaving them in the queue.

---

## 4. Test evidence review

This section lists sniper-related tests and what they actually cover.

### 4.1 `tests/test_sniper_baseline.py`

- `test_runtime_state_backward_compat_without_sniper_fields`
  - Proves:
    - `RuntimeState` validation works when sniper fields are missing from JSON.
    - Sniper fields are defaulted correctly.
  - Strength: Good backward-compat check.

- `test_runtime_state_round_trip_with_sniper_defaults`
  - Proves:
    - `save_state_atomic` + `load_state` preserve sniper fields with defaults.
  - Strength: Good round-trip coverage.

- `test_dashboard_payload_includes_sniper_sections_when_empty`
  - Proves:
    - `build_dashboard_payload` always has `sniper_summary`, `sniper_pending_attempts`, `sniper_recent_decisions`.
  - Strength:
    - Confirms presence of keys but not correctness of values or wiring to state.

### 4.2 `tests/test_sniper_service.py`

- `test_sniper_service_modes_disabled_by_default`
  - Proves:
    - Default config yields `mode in ("disabled","paper","live")`.
    - Service is disabled by default.

- `test_enqueue_rejected_when_disabled`
  - Proves:
    - Enqueue returns `(False, "disabled", 0)` when service disabled.

- `test_enqueue_accepts_valid_when_enabled`
  - Proves:
    - With `sniper_enabled=True`, `sniper_mode="live"`, queue-size>1, enqueue of `"MintA"` succeeds and leaves `SniperManualSeedQueueEntry` in state.

- `test_enqueue_rejects_duplicate_in_queue`
  - Proves:
    - Duplicate mint in queue is blocked with `"duplicate_in_queue"`.

- `test_enqueue_rejects_queue_full`
  - Proves:
    - When queue reached `sniper_max_manual_queue_size`, additional mints are blocked with `"queue_full"`.

- `test_enqueue_rejects_when_open_lot_exists`
  - Proves:
    - If `RuntimeMintState.lots` has an open lot for mint, enqueue is blocked with `"open_lot_exists"`.

### 4.3 Missing tests (explicitly)

There are **no tests** for:

- `SniperService.resolve_pending_attempts` (currently no-op).
- `SniperService.process_candidate_queue` (currently no-op).
- Any `SniperCandidate` pipeline (normalized candidates, risk, scoring).
- Event emission for sniper events.
- Dashboard `sniper_recent_decisions` populated from `state.sniper_last_decisions`.
- Runner cycle behavior with queue entries present and (future) candidate pipeline active.

### 4.4 Verdict on test coverage

- **Well-covered**:
  - RuntimeState backward compatibility and sniper field presence.
  - `SniperService` queue-enqueue gating behavior and reason codes.
  - Dashboard structural presence of sniper sections.

- **Missing coverage** (pre-Phase 1C implementation):
  - Candidate pipeline (construction, normalization, risk, scoring).
  - Decision history and stats updates.
  - Actual runner hook behavior when candidate pipeline exists.
  - Sniper event emission and payload shapes.
  - Dashboard integration with real sniper decisions.

---

## 5. Risk / weakness analysis

This section identifies weaknesses and implementation gaps relative to the spec.

### 5.1 Candidate pipeline is not implemented

- There is **no `SniperCandidate` model**, no normalization, no risk or scoring modules.
- `process_candidate_queue` is a stub.
- Enqueued manual seeds accumulate with no visibility or evaluation.
- **Impact**:
  - Current codebase is still at **Phase 1B**, not Phase 1C.
  - Any higher-level assumptions about a working candidate pipeline are not true yet.

### 5.2 Legacy `_run_sniper_cycle` still present

- `_run_sniper_cycle` includes:
  - Jupiter quote/execute.
  - Confirm fill.
  - Lot creation.
  - Ladder arming.
  - Deployer reputation and sniper failure stats.
- It is no longer called, but:
  - Its presence can confuse future implementers.
  - Any reactivation without strict alignment to new spec would be dangerous.

### 5.3 Dashboard sniper fields are only partially wired

- `sniper_summary`:
  - `enabled`, `mode`, `discovery_enabled` are **hard-coded**; they do not reflect runtime config.
  - `open_sniper_positions_count`, `last_buy_at`, counts from stats are not populated anywhere.
- `sniper_recent_decisions`:
  - Always `[]`; not linked to `state.sniper_last_decisions`.
- **Impact**:
  - Dashboard gives a misleading impression of a disabled sniper even when config turns it on (once we enable it).
  - Operators cannot see decisions even once they are implemented unless this is wired.

### 5.4 No centralized reason-code or outcome helpers

- Reason codes for:
  - enqueue rejection,
  - risk filter reasons (planned),
  - `SniperDecisionOutcome` values,
  - event `reason` fields
  - are currently scattered across:
    - `SniperService` (enqueue reasons as strings).
    - Future risk/scoring code (not yet written).
- **Risk**:
  - String drift and typos likely.
  - Hard to guarantee spec compliance without a shared constants module.

### 5.5 State-machine helpers unused

- `runtime.add_pending_attempt`, `move_attempt_to_history`, `remove_pending_attempt` exist but are unused.
- There is no enforcement at call sites yet because no attempt transition logic exists.
- **Impact**:
  - Currently harmless, but we don’t yet know if future code will consistently use these helpers vs bypass them.

### 5.6 No decision history / stats updates yet

- `SniperStats` and `SniperDecisionEntry` exist but:
  - No code updates `sniper_stats` numbers.
  - `sniper_last_decisions` is never appended to.
- **Impact**:
  - Higher-level reporting in the spec is not backed by implementation.
  - Risk of misalignment between stats and real decisions once logic starts being added ad hoc.

### 5.7 Disabled-mode semantics vs config

- `SniperService.is_enabled` uses:
  - `config.sniper_enabled` and `config.sniper_mode`.
- Dashboard summary always reports `enabled=False`, `mode="disabled"`.
- **Risk**:
  - Even if sniper is legitimately enabled and processing candidates in the future, dashboard will still claim it’s disabled unless updated.

### 5.8 Potential confusion from Literals instead of Enums

- `SniperAttemptState` and `SniperDecisionOutcome` are `Literal[...]` rather than `Enum`.
- This is consistent with some other models, but for a complex state machine:
  - Enums would provide stronger type safety and repr clarity.
  - Literals + scattered strings are brittle.

### 5.9 Lack of explicit “approved candidate” storage

- Spec suggests we may want:
  - Distinct representation of “approved but not yet attempted” candidates.
- Current design:
  - Enqueue queue is present.
  - No place yet reserved for storing approved candidates separate from attempts.
- **Risk**:
  - When we later introduce attempts, we might conflate “approved candidate” with “live attempt” and lose clarity about decisions vs. executions.

---

## 6. Suggestions before next phase

### 6.1 Must fix before moving to quote/attempt/execution

1. **Implement SniperCandidate and candidate pipeline (Phase 1C) properly**
   - Add `SniperCandidate` model to code (`candidate.py` or `models.py`).
   - Implement:
     - `normalize_candidate(...)` in `sniper_engine/normalize.py`.
     - `apply_sniper_risk(candidate, state, config, ...)` in `sniper_engine/risk.py`.
     - `score_candidate(candidate, config, ...)` in `sniper_engine/scoring.py`.
   - Wire `SniperService.process_candidate_queue()` to:
     - Dequeue queue entries.
     - Construct candidates.
     - Normalize → risk → score.
     - Update `sniper_stats`, `sniper_last_decisions`.
     - Emit sniper candidate events.
   - This is the **core missing functionality** that the spec assumes exists.

2. **Wire dashboard sniper decisions**
   - `build_dashboard_payload`:
     - Fill `sniper_recent_decisions` from `state.sniper_last_decisions` (bounded).
     - Set `sniper_summary.last_decision_at` accordingly.
   - Adjust `sniper_summary.enabled` / `mode` to reflect **actual config**:
     - e.g. read `SNIPER_ENABLED` and `SNIPER_MODE` from `Config`.

3. **Remove or quarantine legacy `_run_sniper_cycle`**
   - At minimum:
     - Add an explicit comment that `_run_sniper_cycle` is legacy and **must not** be activated.
   - Better:
     - Move it to a legacy file or delete entirely once all needed pieces are safely reimplemented in the new path.

4. **Centralize reason codes and decision outcomes**
   - Create a small `sniper_engine/constants.py` (or similar) with:
     - Enqueue reasons.
     - Risk reason codes.
     - Allowed `SniperDecisionOutcome` values.
   - Use these in:
     - Service, risk, scoring, and any consumer of decisions/events.

### 6.2 Should improve soon (but not strict blockers)

1. **Convert Literals to Enums or wrap them with helper functions**
   - Either:
     - Define real `Enum` classes for `SniperAttemptState` and `SniperDecisionOutcome`, or
     - Provide helper functions that validate outcome strings and disallow arbitrary values.
   - This will reduce drift and mistakes in future phases.

2. **Add tests around runner-cycle behavior**
   - Add focused tests that:
     - Run `run_bot` for `single_cycle=True` with sniper disabled → assert state not changed by sniper.
     - After implementing candidate pipeline:
       - With enabled sniper and queued entries, assert:
         - Queue drained.
         - Decisions/events recorded.
         - No attempts/lots/ladder changes.

3. **Strengthen mint validation in `enqueue_manual_seed`**
   - Today, only non-empty string is enforced.
   - Should at least ensure it looks like a base58 Solana pubkey (length and charset), to catch obvious operator typos.

4. **Explicit “approved candidate” holding structure**
   - Consider adding a `sniper_approved_candidates` or similar field in `RuntimeState` to store:
     - Candidates that passed risk+scoring but have not yet led to an attempt.
   - This will make the transition from candidate pipeline → execution pipeline clearer and testable.

### 6.3 Nice to have later

1. **Event payload helper layer**
   - Small helpers like `emit_sniper_candidate_event(...)` to:
     - Enforce common payload fields.
     - Reduce duplication and risk of inconsistent event shapes.

2. **Richer SniperService return objects**
   - Instead of tuples, small typed result objects (e.g. `EnqueueResult`) with named attributes and reason enums.

3. **Dashboard summary stats integration**
   - Once `SniperStats` is updated properly, show:
     - Counts of blocked vs passed candidates.
     - Counts per reason class.

---

## 7. Final readiness verdict

### Verdict: **Yes, with corrections**

The current codebase is **safe** to build execution logic on **only after** the following corrections are made:

1. **Implement the full Phase 1C candidate pipeline** (SniperCandidate model, normalization, risk, scoring) and wire it into `SniperService.process_candidate_queue`.
2. **Wire decision history and dashboard** so that:
   - `sniper_last_decisions` is updated.
   - `sniper_recent_decisions` and `sniper_summary.last_decision_at` reflect real decisions.
3. **Clarify / remove the legacy `_run_sniper_cycle`** to avoid accidentally reactivating the old sniper path.
4. **Centralize reason codes and outcomes** to match the spec and avoid string drift.

Until those pieces are in place, the sniper code is effectively at **Phase 1B** (control-plane scaffolding) rather than a functioning Phase 1C candidate pipeline.

Once these corrections are implemented and covered by tests:

- The foundation will be clean and transparent.
- Execution logic (quote → attempt → reconciliation → lot metadata → ladder arming) can be layered on top without fighting legacy behavior or missing observability.

