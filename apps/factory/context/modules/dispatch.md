# Dispatch — gating story-handler dispatch via modes, caps, and rejection reasons

## Overview

The dispatch gate sits in front of orchestrator handler execution. On each `tick()`, the orchestrator resolves the next handler for a `StoryRecord`, derives a `job_kind`, and calls `factory.settings.enforcer.can_dispatch(...)` before invoking the handler. The gate uses static settings from `factory_settings.yaml` plus mutable runtime mode from `state/factory.db`, and may reject work with a structured `rejected_reason` that is recorded on the story and surfaced via operator tooling (`factory why` mentioned in source).

## Key concepts

- **`can_dispatch` as pre-handler guard**
  - `factory.chain.orchestrator` imports `can_dispatch` from `factory.settings.enforcer` and documents it as the Phase 3 settings enforcer placed “in front of every handler dispatch.”
  - If dispatch is rejected, the story is skipped for that tick and a structured `rejected_reason` is recorded.

- **Static settings vs mutable runtime mode**
  - `factory_settings.yaml` defines caps, rate limits, and the allowed mode names.
  - The current mode is not stored in YAML; it lives in SQLite table `factory_state` in `state/factory.db` and is read by `get_mode(...)`.

- **Mode catalog and defaults**
  - `factory.settings.loader.ModesConfig.available` defaults to:
    - `normal`
    - `fix-only`
    - `drain-reviews`
    - `paused`
    - `exploratory`
    - `deploy-frozen`
    - `ux-audit-only`
  - `ModesConfig.default` defaults to `normal`.
  - `load_settings(...)` validates that `modes.default` is present in `modes.available`.

- **Caps configuration**
  - `CapsConfig` defines:
    - `global_concurrent_agents`
    - `per_repo_concurrent_agents`
    - `daily_spend_usd`
    - `hourly_spend_usd`
  - Orchestrator source explicitly says `can_dispatch` reads “current factory mode + caps” from YAML and local DB, and may reject for examples like `daily_spend_cap_exceeded`.

- **Rate-limit configuration**
  - `RateLimitsConfig` defines operation-specific hourly/daily limits, including:
    - `pm_invocations_per_hour`
    - `ralph_runs_per_day`
    - `bug_hunter_runs_per_day`
    - `security_runs_per_day`
    - `ux_auditor_runs_per_day`
    - `factory_improver_runs_per_day`
  - The exact mapping from these fields to `can_dispatch` decisions is not confirmed in provided source, but orchestrator comments say `blocked_by_caps` includes “mode, cap, rate-limit”.

- **Handler-to-`job_kind` translation**
  - The orchestrator uses `_dispatch_for_story(...)` to resolve the handler name from `StoryState`.
  - It then uses `_resolve_job_kind(...)` to derive the `job_kind` passed to `can_dispatch`.
  - For bug-scoped work, certain handler kinds gain a `-bug` suffix:
    - bug-aware kinds are `sm`, `test_design`, `test_impl`, `dev`, `review`
    - bug detection uses `direction.type_tag == "bug"` or `story.scope == "bug"`
    - result examples: `dev-bug`, `review-bug`

- **Fix-only mode compatibility**
  - `_resolve_job_kind(...)` exists specifically so `fix-only` mode can allow bug work while blocking feature work.
  - The orchestrator comment says the enforcer’s `_mode_blocks` understands the suffixed kinds; that `_mode_blocks` implementation is not included here, but the contract is explicit.

- **Observable rejection accounting**
  - `TickSummary.blocked_by_caps` counts stories rejected by `can_dispatch`.
  - `TickSummary.rejected` stores tuples of `(story_slug, rejected_reason)`.
  - Rejected stories are distinct from `stories_blocked`, which refers to mid-chain `BLOCKED` state rather than front-door dispatch denial.

## Key files

- **`factory/chain/orchestrator.py`**
  - Main integration point for dispatch gating: imports `can_dispatch`, resolves handler names and `job_kind`, and records rejection outcomes in tick summary and story state.

- **`factory/settings/loader.py`**
  - Defines `FactorySettings` and the settings schema used by the enforcer: `caps`, `rate_limits`, `modes`, and related defaults; loads and caches `factory_settings.yaml`.

- **`factory/settings/modes.py`**
  - Persists and retrieves the current runtime mode via `FactoryState` in SQLite; `get_mode(...)` initializes mode from YAML default on first read, and `set_mode(...)` validates against `modes.available`.

- **`factory/settings/enforcer.py`**
  - Referenced by orchestrator as the owner of `can_dispatch` and `_mode_blocks`, but implementation not provided in the source bundle. It is the decisive module for actual allow/reject logic and rejection reason generation.

- **`factory/settings/spend.py`**
  - Imported by orchestrator (`hour_spend_usd`, `today_spend_usd`), indicating spend accounting is part of the broader dispatch/cap system, though usage details are not shown in the provided excerpt.

## Failure modes

- **Invalid mode configured in YAML**
  - If `factory_settings.yaml` sets `modes.default` to a value not listed in `modes.available`, `load_settings(...)` raises `ValueError`.
  - Symptom: settings load fails before normal dispatch logic can run.

- **Operator attempts to set an unsupported mode**
  - `set_mode(...)` raises `ValueError` when `new_mode` is not in `settings.modes.available`.
  - Symptom: runtime mode remains unchanged; any expected gating behavior does not take effect.

- **Stale settings due to loader memoization**
  - `load_settings(...)` caches parsed settings per root for the life of the process.
  - If `factory_settings.yaml` changes and `reload_settings(...)` is not called in-process, `can_dispatch` may use stale caps or allowed mode definitions.
  - Symptom: observed dispatch decisions do not match the current YAML on disk.

- **Unexpected mode initialization on first read**
  - `get_mode(...)` lazily creates `factory_state.id=1` using `settings.modes.default` if no row exists.
  - If an operator expects an already-set runtime mode in a fresh environment, the factory silently starts in the YAML default instead.
  - Symptom: dispatch behavior follows `normal` (or configured default) until explicitly changed.

- **Bug/feature classification mismatch**
  - `fix-only` behavior depends on `_resolve_job_kind(...)` correctly identifying bug work from `direction.type_tag` or `story.scope`.
  - If either field is missing or incorrect, a bug-fix story may be treated as normal feature work and rejected, or feature work may be incorrectly suffixed.
  - Symptom: wrong `rejected_reason` under `fix-only`, especially for `sm`, `test_design`, `test_impl`, `dev`, or `review`.

- **Cap or rate-limit rejection halts progress without changing chain state**
  - Rejected stories are skipped for the tick rather than advanced.
  - Symptom: stories remain in the same `StoryState`, accumulate `rejected_reason`, and `TickSummary.blocked_by_caps` rises even though handlers are not failing.

## Escalation paths

When dispatch is denied, the orchestrator does not invoke the handler. Instead, it records a structured `rejected_reason` on the `StoryRecord`, adds an entry to `TickSummary.rejected`, increments `TickSummary.blocked_by_caps`, and skips the story for that tick. This is the primary non-fatal containment path for mode/cap/rate-limit enforcement.

Operator visibility is expected through `factory why`, which the orchestrator docstring explicitly names as the inspection path for rejection reasons. The exact CLI implementation is not provided here, but the intended escalation loop is: inspect `rejected_reason`, determine whether the block is due to current mode, spend cap, concurrency, or rate limit, then intervene by changing runtime mode (`set_mode(...)`) and/or adjusting `factory_settings.yaml`.

If the problem is configuration-based:
- update `factory_settings.yaml` for caps or allowed modes
- call `reload_settings(...)` in-process where applicable, or restart the process if the caller does not reload settings
- re-run `tick()`

If the problem is runtime-mode-based:
- use `set_mode(new_mode, ...)` to persist a new `factory_state.mode`
- subsequent dispatch checks should use the new mode via `get_mode(...)`

State transitions on rejection are not confirmed in the provided source beyond “records the reason on the `StoryRecord` and skips the story for this tick.” There is no confirmed transition to `BLOCKED` for `can_dispatch` denials; in fact, `stories_blocked` is tracked separately for mid-chain `BLOCKED` state. Likewise, no human notification channel is confirmed in the provided excerpts beyond operator inspection tooling.