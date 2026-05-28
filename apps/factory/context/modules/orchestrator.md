# Orchestrator — tick loop and per-story handler dispatch

## Overview

The orchestrator drives in-flight `StoryRecord`s one step at a time. A single `tick()` pass inspects each non-terminal story for an app, determines the next handler from the story’s current `state` (plus a small amount of story metadata), checks dispatch policy via the settings enforcer, and invokes at most one handler for that story.

The core mechanics relevant here are: `_dispatch_for_story()` chooses a handler name or returns `None`; `_invoke_handler()` maps that name to a concrete function in `factory.chain.handlers`; and the tick loop records advancement, rejections, blocking, errors, and auto-merge outcomes in `TickSummary`. The orchestrator is explicitly one-handler-per-story-per-tick, not full-chain execution.

## Key concepts

- **One-tick, one-handler progression**
  - The module docstring states that one `tick()` advances each story by exactly one handler invocation, not the whole chain.

- **State-driven dispatch with two special branches**
  - `_DISPATCH` is the default state→handler map.
  - `_dispatch_for_story(story)` adds two dynamic branches:
    - `StoryState.STORY_CREATED` dispatches by `story.chain_kind`:
      - `"docs"` → `"docs_sm"`
      - otherwise → `"sm"`
    - `StoryState.TESTS_RED` dispatches by `story.harness_precheck_passed`:
      - `False` → `"harness_precheck"`
      - `True` → `"dev"`

- **Docs chain vs TDD chain**
  - `StoryRecord.chain_kind` defaults to `"tdd"`.
  - Docs-only stories start with `docs_sm`, then `docs_onboarder`, then `docs_enforcer`, skipping the red→green test loop.

- **Harness precheck as a one-shot gate**
  - `StoryRecord.harness_precheck_passed` defaults to `False`.
  - On first arrival at `TESTS_RED`, the orchestrator sends the story to `harness_precheck`; after a pass, subsequent `TESTS_RED` visits go straight to `dev`.
  - `DEV_RETRY` always dispatches directly to `dev`.

- **Settings enforcement before dispatch**
  - The orchestrator runs `can_dispatch(...)` ahead of handler invocation.
  - If dispatch is rejected, the orchestrator stores a structured reason in `StoryRecord.last_rejection_reason`, skips the story for that tick, and surfaces the reason via `factory why` (per module docstring).

- **Bug-aware job kinds**
  - `_resolve_job_kind(...)` appends `-bug` to certain handler kinds (`"sm"`, `"test_design"`, `"test_impl"`, `"dev"`, `"review"`) when the work is bug-typed.
  - Bug typing is derived from `direction.type_tag == "bug"` or `story.scope == "bug"`.
  - This affects what `can_dispatch` sees, especially in `fix-only` mode.

- **Handler execution is indirect**
  - `_invoke_handler(name, ...)` is the dispatch fan-out point from string names like `"sm"` or `"deploy"` to concrete handler functions in `factory.chain.handlers`.
  - The excerpt confirms mappings for at least `sm`, `test_design`, `test_impl`, and `harness_precheck`; the rest of the function is truncated, but `_DISPATCH` implies additional names such as `dev`, `review`, `tech_writer`, `docs_enforcer`, `docs_sm`, `docs_onboarder`, and `deploy`.

- **TickSummary as the primary orchestration audit**
  - `TickSummary` captures:
    - `stories_advanced`
    - `blocked_by_caps`
    - `stories_blocked`
    - `handler_runs` as `(story_slug, from_state, to_state)`
    - `rejected` as `(story_slug, rejected_reason)`
    - `errors` as `(story_slug, error)`
    - `merges` as `list[MergeAction]`
    - `halted` / `halt_reason`

## Key files

- `factory/chain/orchestrator.py`
  - Main orchestrator logic: `tick()`, `_dispatch_for_story()`, `_invoke_handler()`, job-kind resolution, `TickSummary`, and auto-merge integration.

- `factory/chain/state_machine.py`
  - Defines `StoryState`, `StoryRecord`, and chain semantics the orchestrator dispatches against.

- `factory/chain/handlers.py`
  - Concrete handler implementations called by `_invoke_handler()`; not provided here, but this is where side effects and state persistence occur.

- `factory/settings/enforcer.py`
  - Provides `can_dispatch(...)`, which can block a handler based on mode/caps/rate limits and emit a structured rejection reason.

- `factory/settings/loader.py`
  - Loads factory settings consumed by the dispatch gate.

- `factory/settings/modes.py`
  - Exposes `get_mode`, part of the mode-aware dispatch policy path.

- `factory/chain/auto_merge.py`
  - Supplies `auto_merge_tick()` and `MergeAction`; orchestrator appends end-of-tick merge decisions to `TickSummary.merges`.

- `factory/chain/event_log.py`
  - Provides `log_story_event`, used for orchestration/event audit logging.

## Failure modes

- **No handler for current state**
  - If `_dispatch_for_story()` returns `None` for a story state not present in `_DISPATCH` and not covered by the special branches, the story will not be advanced by a handler that tick.
  - Observable symptom: story remains in the same state; likely no `handler_runs` entry for that story.
  - Exact fallback/recording behavior is not confirmed in the provided source excerpt.

- **Incorrect chain routing at `STORY_CREATED`**
  - If `story.chain_kind` is wrong or missing expected values, `STORY_CREATED` defaults to `"sm"` unless it is exactly `"docs"`.
  - Observable symptom: a docs story enters the TDD path instead of the docs path.

- **Harness precheck flag drift**
  - If `harness_precheck_passed` is never set after a successful precheck, `TESTS_RED` will repeatedly dispatch `harness_precheck` instead of `dev`.
  - If it is incorrectly set `True`, the precheck is skipped and the story goes straight to `dev`.
  - Observable symptom: repeated precheck runs or unexpected dev dispatch from `TESTS_RED`.

- **Dispatch blocked by settings/caps**
  - `can_dispatch(...)` may reject a story due to mode, spend caps, or rate limits.
  - Observable symptom: `TickSummary.blocked_by_caps` increments, `TickSummary.rejected` contains `(story_slug, rejected_reason)`, and `StoryRecord.last_rejection_reason` is populated.

- **Bug/fix-only policy mismatch**
  - `_resolve_job_kind()` only adds `-bug` for `_BUG_AWARE_HANDLER_KINDS`. If bug classification is wrong (`direction.type_tag` absent/wrong and `story.scope` not `"bug"`), fix-only mode may reject legitimate bug work.
  - Observable symptom: rejection reason from `can_dispatch`, despite operator expectation that the story is a bug fix.

- **Handler exception or error result**
  - `_invoke_handler()` calls into side-effecting handlers that may fail or raise.
  - The request asks specifically about success/failure/exception behavior, but the relevant body of `tick()` and the full `_invoke_handler()` are truncated. The exact state transitions, error field updates, and event logging on handler failure/exception are therefore not fully confirmed from the provided source.
  - What is confirmed is that `TickSummary.errors` exists for `(story_slug, error)` recording.

- **Auto-merge/deploy progression not occurring**
  - Stories in `DEPLOY_PENDING` rely on orchestrator dispatch to `"deploy"`.
  - If the handler mapping or state transition into `DEPLOY_PENDING` is broken, post-merge deploy does not run.
  - Observable symptom: story remains at `deploy_pending`; `TickSummary.merges` may show merge decisions without downstream deployment advancement.

## Escalation paths

When a story is rejected before dispatch, the orchestrator does not invoke the handler. Instead, it records a structured rejection reason on `StoryRecord.last_rejection_reason`, includes the rejection in `TickSummary.rejected`, and the operator can inspect why via `factory why` (explicitly stated in the orchestrator docstring). This is the main non-exception policy escalation path.

When tests are environmentally broken at the harness precheck stage, the state machine design routes the story to `StoryState.BLOCKED_TESTS_NEED_CLARIFICATION` via `EVENT_HARNESS_PRECHECK_FAIL`. That is an operator-attention bucket rather than something the dev handler should burn retries on. The orchestrator tracks blocked stories via `TickSummary.stories_blocked`.

For deploy failures, `StoryState` includes `BLOCKED_DEPLOY_FAILED`, indicating a blocked escalation state for post-merge deployment problems. The exact transition logic is not shown in the provided orchestrator excerpt, but that state is the defined failure sink in the state machine.

For hard orchestrator-level failure, `TickSummary` includes `errors`, plus `halted` and `halt_reason`. The docstring notes Phase 7 early exit due to factory halt. The exact notifier, state mutation, and operator workflow for halt/error conditions are not confirmed in the provided source excerpt, but the summary structure indicates the orchestrator is expected to surface these as end-of-tick control-plane signals.

On normal success, a story should move from its prior state to a new state and appear in `TickSummary.handler_runs` as `(story_slug, from_state, to_state)`, with `stories_advanced` incremented. On handler failure/exception, the exact distinction between “error recorded,” “story left in place,” and “story transitioned to a blocked/retry state” depends on the tick-loop code and handler contracts, which are not fully visible in the provided source.