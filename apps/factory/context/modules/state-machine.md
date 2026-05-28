# state-machine — story lifecycle and rollback edges

## Overview

The story state machine in `factory/chain/state_machine.py` is the canonical lifecycle for a `StoryRecord` persisted in `state/factory.db.stories`. It is intentionally pure: `advance(story, event, payload) -> StoryState` decides the next state, while the orchestrator and handlers perform side effects and persist updated fields. Two chain variants share the same enum: the default `tdd` path and a shorter `docs` path, converging at `DOCS_ENFORCER_CHECK` and the PR/deploy tail.

## Key concepts

- `StoryState` is a `StrEnum` covering all in-flight, done, retry, and blocked states for both `tdd` and `docs` chains.
- `StoryRecord.state` stores the current state as a string; `StoryRecord.chain_kind` (`"tdd"` or `"docs"`) determines the initial dispatch from `STORY_CREATED`.
- Dispatch is state-driven in `factory/chain/orchestrator.py:_DISPATCH`, with special-case logic in `_dispatch_for_story()` for:
  - `STORY_CREATED` → `sm` or `docs_sm`
  - `TESTS_RED` → `harness_precheck` first unless `harness_precheck_passed=True`, then `dev`
- The state machine is event-driven. Source confirms event constants such as `EVENT_SM_STARTED`, `EVENT_SM_DONE`, `EVENT_TEST_DESIGN_DONE`, `EVENT_TESTS_RED`, `EVENT_HARNESS_PRECHECK_PASS`, and `EVENT_HARNESS_PRECHECK_FAIL`.
- Rollback/rework paths are explicit states, not implicit rewinds:
  - `DEV_RETRY` loops back into dev work
  - `REVIEWER_REQUESTED_CHANGES` exists for reviewer-driven rework
  - blocked conditions land in terminal/manual-attention buckets like `BLOCKED_TESTS_NEED_CLARIFICATION`
- `harness_precheck_passed` is a per-story guard preventing repeated harness checks; precheck runs once after tests are authored and before the first dev attempt.
- The docs-only chain skips red/green execution states entirely: `docs_sm` → `docs_onboarder` → `docs_enforcer` → PR tail.
- Terminal/near-terminal delivery states include `PR_OPEN`, `CI_PENDING`, `CI_GREEN`, `READY_FOR_MERGE`, `DEPLOY_PENDING`, `DEPLOYED`, plus blocked terminal states.

## Key files

- `factory/chain/state_machine.py` — defines `StoryState`, `StoryRecord`, and the event vocabulary used to compute legal transitions.
- `factory/chain/orchestrator.py` — maps current state to handler dispatch and contains the only confirmed branching logic for `chain_kind` and `harness_precheck_passed`.
- `factory/chain/handlers.py` — not provided here, but referenced by orchestrator as the side-effect layer that consumes next-state decisions and persists outputs.
- `factory/chain/auto_merge.py` — referenced by orchestrator for post-PR merge/deploy progression; exact state transitions not confirmed in provided source.
- `factory/chain/event_log.py` — referenced by orchestrator for story event logging; useful for reconstructing transition history.

## Failure modes

- Invalid dispatch for a valid state: states like `SM_IN_PROGRESS`, `TEST_DESIGN_IN_PROGRESS`, `TEST_IMPLEMENTATION_IN_PROGRESS`, `DEV_IN_PROGRESS`, `REVIEWER_IN_PROGRESS`, `TECH_WRITER_IN_PROGRESS`, `DOCS_SM_IN_PROGRESS`, `DOCS_ONBOARDER_IN_PROGRESS`, `DOCS_ENFORCER_CHECK`, `PR_OPEN`, `CI_PENDING`, `CI_GREEN`, and `READY_FOR_MERGE` do not appear in `_DISPATCH`. They likely advance only via webhook/completion events or other code paths; if those signals never arrive, the story stalls.
- Harness/environment breakage before dev: from `TESTS_RED`, the first dispatch is `harness_precheck` unless `harness_precheck_passed=True`. If pytest collection exits with environmental failure, the story should transition to `BLOCKED_TESTS_NEED_CLARIFICATION` instead of burning dev retries.
- Mis-set `harness_precheck_passed`: if persisted incorrectly as `True`, a story in `TESTS_RED` skips the precheck and goes directly to `dev`; if never set after success, the orchestrator may repeatedly choose `harness_precheck`.
- Chain-kind mismatch: a docs story with `chain_kind="tdd"` enters the TDD path; a code story with `chain_kind="docs"` skips test design/implementation/dev entirely. No validation is shown in provided source.
- Rejection before transition: `can_dispatch()` can block handler execution for mode/cap/rate-limit reasons, leaving the story in place and recording `last_rejection_reason`. Symptom is no state transition during a tick.
- Blocked deploy path: `BLOCKED_DEPLOY_FAILED` exists as a state, implying deploy failure can leave the chain halted after merge. Exact triggering events are not confirmed in provided source.

## Escalation paths

When this component “fails,” the actual escalation pattern depends on where the failure occurs:

- Dispatch-time policy rejection:
  - `factory/chain/orchestrator.py` calls `can_dispatch()`.
  - On rejection, the orchestrator does not advance state; it records `StoryRecord.last_rejection_reason` and includes the item in `TickSummary.rejected`.
  - Operator intervention is via settings/caps inspection (`factory why` is mentioned in the orchestrator docstring).

- Mid-chain blocked conditions:
  - Test-set or harness/environment failures are explicitly funneled to `BLOCKED_TESTS_NEED_CLARIFICATION`.
  - Deploy failures can land in `BLOCKED_DEPLOY_FAILED`.
  - These are the operator-attention buckets; the story is no longer on a normal auto-advance path and likely requires human clarification, environment repair, or manual state correction.

- Retry/rework instead of hard failure:
  - Dev failures are modeled with `DEV_RETRY`, which the orchestrator dispatches back to `dev`.
  - Reviewer-driven rework is modeled with `REVIEWER_REQUESTED_CHANGES`, but the outbound transition from that state is not confirmed in provided source.
  - This means rollback is generally logical rollback to a prior work phase, not database/history rewind.

- Stalled in-progress states:
  - In-progress states are expected to be exited by completion events/webhooks rather than `_DISPATCH`.
  - If a handler crashes after marking `*_IN_PROGRESS`, or if a webhook/completion event is lost, the story may remain stranded.
  - Observable symptom: repeated ticks do nothing because no handler is mapped for that state.
  - Recommended operator action (behavior not fully confirmed in provided source): inspect event log, handler result fields (`sm_result_json`, `test_plan_json`, `reviewer_result_json`, etc.), and manually repair or replay the transition.

### Confirmed state inventory and likely outbound ownership

Below is the most source-grounded summary possible from the provided excerpts.

- `STORY_CREATED`
  - Outbound owner: orchestrator `_dispatch_for_story()`
  - `chain_kind=="tdd"` → dispatch `sm`
  - `chain_kind=="docs"` → dispatch `docs_sm`

- `SM_IN_PROGRESS`
  - Exists for active SM work
  - Outbound transition mechanism not confirmed in provided source, but likely by SM handler completion event

- `SM_DONE`
  - Outbound owner: orchestrator `_DISPATCH`
  - Dispatches `test_design`

- `TEST_DESIGN_IN_PROGRESS`
  - Outbound not confirmed; likely handler completion event

- `TEST_DESIGN_DONE`
  - Outbound owner: orchestrator `_DISPATCH`
  - Dispatches `test_impl`

- `TEST_IMPLEMENTATION_IN_PROGRESS`
  - Outbound not confirmed; likely handler completion event

- `TESTS_RED`
  - Outbound owner: orchestrator `_dispatch_for_story()`
  - If `harness_precheck_passed=False` → dispatch `harness_precheck`
  - Else → dispatch `dev`

- `HARNESS_PRECHECK_IN_PROGRESS`
  - Outbound not confirmed in transition table, but comments confirm:
    - PASS → `DEV_IN_PROGRESS`
    - FAIL → `BLOCKED_TESTS_NEED_CLARIFICATION`

- `DEV_IN_PROGRESS`
  - Outbound not confirmed in dispatch table; likely leaves via dev-completion event

- `DEV_RETRY`
  - Outbound owner: orchestrator `_DISPATCH`
  - Dispatches `dev`

- `TESTS_GREEN`
  - Outbound owner: orchestrator `_DISPATCH`
  - Dispatches `review`

- `REVIEWER_IN_PROGRESS`
  - Outbound not confirmed; likely reviewer-completion event

- `REVIEWER_DONE`
  - Outbound owner: orchestrator `_DISPATCH`
  - Dispatches `tech_writer`

- `REVIEWER_REQUESTED_CHANGES`
  - Rework/rollback state
  - Outbound transition not confirmed in provided source

- `TECH_WRITER_IN_PROGRESS`
  - Outbound not confirmed; likely handler completion event

- `TECH_WRITER_DONE`
  - Outbound owner: orchestrator `_DISPATCH`
  - Dispatches `docs_enforcer`

- `DOCS_SM_IN_PROGRESS`
  - Outbound not confirmed; likely docs SM completion event

- `DOCS_SM_DONE`
  - Outbound owner: orchestrator `_DISPATCH`
  - Dispatches `docs_onboarder`

- `DOCS_ONBOARDER_IN_PROGRESS`
  - Outbound not confirmed; likely handler completion event

- `DOCS_ONBOARDER_DONE`
  - Outbound owner: orchestrator `_DISPATCH`
  - Dispatches `docs_enforcer`

- `DOCS_ENFORCER_CHECK`
  - Shared convergence point for both chains
  - Outbound transition not confirmed in provided source; likely toward PR creation/open

- `PR_OPEN`
  - Exists as post-enforcer state
  - Outbound transition to CI states not confirmed in provided source

- `CI_PENDING`
  - Outbound not confirmed; likely webhook-driven

- `CI_GREEN`
  - Outbound not confirmed; likely auto-merge or merge eligibility evaluation

- `READY_FOR_MERGE`
  - Outbound not confirmed in excerpts; likely consumed by auto-merge logic

- `DEPLOY_PENDING`
  - Outbound owner: orchestrator `_DISPATCH`
  - Dispatches `deploy`

- `DEPLOYED`
  - Terminal success state

- `BLOCKED_TESTS_NEED_CLARIFICATION`
  - Blocked/manual-attention state; terminal or semi-terminal from normal automation’s perspective

- `BLOCKED_DEPLOY_FAILED`
  - Blocked/manual-attention state after deploy failure

Overall, the rollback model is conservative and stateful: dev issues loop through `DEV_RETRY`, reviewer rework uses `REVIEWER_REQUESTED_CHANGES`, environmental test failures short-circuit to `BLOCKED_TESTS_NEED_CLARIFICATION`, and policy/cap failures do not transition state at all. Exact event-to-state edges for many `*_IN_PROGRESS`, PR, and CI states are not fully confirmed in the provided source excerpts.