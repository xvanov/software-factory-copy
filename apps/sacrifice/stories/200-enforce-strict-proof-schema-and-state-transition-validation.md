# Story

## Title
Enforce strict proof schema and state-transition validation — broad read

## Scope
backend

## Summary
Implement the full backend security slice for proof submission and lifecycle enforcement: validate proof payloads against goal-type-specific schema before persistence, reject illegal proof/status transitions with explicit API behavior and test coverage, and emit audit events for both accepted and rejected validation outcomes.

# Acceptance Criteria
- [ ] Server validates proof payload against goal-type-specific schema before persistence
- [ ] Illegal proof/status transitions are rejected with test coverage
- [ ] Audit events capture rejected and accepted proof validation outcomes

### Testable Claims (EARS)
AC1.1: WHEN a proof submission request is processed, THE server SHALL validate the proof payload against the goal-type-specific schema before persistence
AC1.2: WHEN a proof submission payload does not satisfy the goal-type-specific schema, THE server SHALL prevent persistence of that proof payload
AC2.1: WHEN a proof or status transition is illegal, THE server SHALL reject the transition
AC2.2: WHEN an illegal proof or status transition is attempted, THE system SHALL provide test coverage proving rejection behavior
AC3.1: WHEN proof validation is accepted, THE audit event system SHALL capture the accepted proof validation outcome
AC3.2: WHEN proof validation is rejected, THE audit event system SHALL capture the rejected proof validation outcome

# Tasks / Subtasks
- [ ] Map current proof submission path and goal-type schema seam
  - [ ] Identify request model(s) and route(s) that persist proof bodies
  - [ ] Identify registry metadata already available for goal-type-specific schema lookup
  - [ ] Confirm current proof/status fields and transition write paths
- [ ] Add goal-type proof schema enforcement before persistence
  - [ ] Resolve goal type from submitted goal/proof context
  - [ ] Load goal-type-specific schema from existing registry/discovery seam
  - [ ] Validate payload before DB write
  - [ ] Return explicit rejection on invalid payload
  - [ ] Prevent invalid proof persistence
- [ ] Add explicit proof/status transition guards
  - [ ] Enumerate allowed transitions from current model/state usage
  - [ ] Centralize legality checks in backend lifecycle path
  - [ ] Reject illegal transitions before mutation/persistence
  - [ ] Keep transition rules deterministic and observable via API response
- [ ] Add audit event capture for validation outcomes
  - [ ] Emit event for accepted validation path
  - [ ] Emit event for rejected validation path
  - [ ] Persist enough event detail to distinguish accept vs reject outcome
- [ ] Add backend test coverage
  - [ ] Valid proof payload accepted and persisted
  - [ ] Invalid proof payload rejected and not persisted
  - [ ] Legal proof/status transitions accepted
  - [ ] Illegal proof/status transitions rejected
  - [ ] Accepted validation outcome audited
  - [ ] Rejected validation outcome audited
- [ ] Preserve existing goal-type registry reuse
  - [ ] Avoid parallel proof validation mechanism
  - [ ] Keep implementation aligned with discovered goal-type metadata

# Dev Notes
## Direction artifacts
[flow.md: none]

[api_spec.md: none]

## Direction acceptance criteria (verbatim)
- [ ] Server validates proof payload against goal-type-specific schema before persistence
- [ ] Illegal proof/status transitions are rejected with test coverage
- [ ] Audit events capture rejected and accepted proof validation outcomes

## Implementation notes
- This story is the broad-read consolidation of the PM decomposition. It intentionally spans all three child-story concerns in one backend story file because this invocation was assigned the broad-read record slug.
- Reuse existing goal-type registry/discovery where possible rather than introducing a parallel proof-validation mechanism.
- Keep transitions explicit and testable; avoid vibes-based "invalid state" handling.
- Audit logging must cover both accept and reject outcomes, not only failures.
- If the current codebase lacks a dedicated audit-event persistence abstraction, extend the existing persistence path rather than adding speculative infrastructure.
- If acceptance/rejection currently happens deep inside route logic, prefer extracting deterministic validation/transition functions that can be covered by focused tests and exercised through API tests.

## Context pointers for Dev / Test-Designer
- [Source: context/project.md#Identity]
- [Source: context/project.md#Stack]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on backend API or goal lifecycle]
- [Source: context/navigation.md#When working on generated goal types or registry compatibility]

## Likely code touchpoints
- `backend/app/routes/goals.py`
- `backend/app/goal_types/registry.py`
- `backend/app/schemas/goal.py`
- `backend/app/models/proof.py`
- `backend/app/models/goal.py`
- Backend tests covering goal submission / lifecycle / audit behavior

## Gaps / callouts
- The supplied prelude references `context/current-state.md`, `context/modules/backend.md`, and `context/glossary.md`, but those files were not present in the provided context bundle. Do not assume sections from those files without reloading them if they become available later.
- `flow.md` and `api_spec.md` were explicitly absent in the direction.

# References
- `backend/app/routes/goals.py`
- `backend/app/goal_types/registry.py`
- `backend/app/schemas/goal.py`
- `backend/app/models/goal.py`
- `backend/app/models/proof.py`
- `backend/app/main.py`
- `backend/app/core/celery_app.py`
- `backend/cli/main.py`
- `PRD.md`
- `PROMPT.md`

# Dev Agent Record
## Agent Model Used
- TBD

## Debug Log References
- TBD

## Completion Notes List
- TBD

## File List
- TBD

# Senior Developer Review
## Reviewer
- TBD

## Review Notes
- TBD

## Outcome
- TBD

# Review Follow-ups
- [ ] TBD
