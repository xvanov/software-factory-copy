# Story
**Title:** Enforce strict proof schema and state-transition validation — narrow read
**Slug:** enforce-strict-proof-schema-and-state-transition-validation
**Scope:** backend

## Acceptance Criteria
- [ ] Server validates proof payload against goal-type-specific schema before persistence
- [ ] Illegal proof/status transitions are rejected with test coverage
- [ ] Audit events capture rejected and accepted proof validation outcomes

### Testable Claims (EARS)
AC1.1: WHEN a proof submission is received, GIVEN the goal has a goal type with a specific proof schema, THE server SHALL validate the proof payload against the goal-type-specific schema before persistence
AC1.2: WHEN a proof submission payload does not satisfy the goal-type-specific schema, THE server SHALL reject the submission before persistence
AC2.1: WHEN a proof or status change request would cause an illegal proof/status transition, THE server SHALL reject the illegal proof/status transition
AC2.2: WHEN illegal proof/status transitions are handled, THE system SHALL provide test coverage for the rejection behavior
AC3.1: WHEN proof validation succeeds, THE audit event system SHALL capture the accepted proof validation outcome
AC3.2: WHEN proof validation fails or a proof/status transition is rejected, THE audit event system SHALL capture the rejected proof validation outcome

## Tasks/Subtasks
- [ ] Identify current proof submission persistence path in backend route/service/model code
- [ ] Identify goal-type registry/schema source already available for proof validation
- [ ] Add submission-path validation before proof persistence
- [ ] Reject invalid proof payloads before database write
- [ ] Define explicit allowed proof/status transitions in backend lifecycle logic
- [ ] Reject illegal proof/status transitions through API-visible errors
- [ ] Emit audit events for accepted validation outcomes
- [ ] Emit audit events for rejected validation outcomes
- [ ] Add backend tests for valid proof payload acceptance
- [ ] Add backend tests for invalid proof payload rejection
- [ ] Add backend tests for illegal proof/status transition rejection
- [ ] Add backend tests proving audit capture for accept and reject paths

## Dev Notes
- Narrow-read scope: one backend story covering all three direction acceptance criteria, limited to server-side proof submission/state-transition/audit behavior only. No frontend, CLI, worker, or doc changes.
- `flow.md` not provided by direction.
- `api_spec.md` not provided by direction.
- Reuse existing goal-type registry/discovery seam; do not introduce a parallel proof-validation mechanism.
- Keep transitions explicit and testable; avoid implicit or catch-all invalid-state handling.
- If the current codebase lacks a formal audit-event abstraction, implement the smallest backend-consistent persistence/emission path that satisfies acceptance and is directly testable.

### Context Pointers
- [Source: context/project.md#Identity]
- [Source: context/project.md#Stack]
- [Source: context/project.md#Top-level layout]
- [Source: context/project.md#Active constraints]

### Verbatim Direction Acceptance Criteria
- [ ] Server validates proof payload against goal-type-specific schema before persistence
- [ ] Illegal proof/status transitions are rejected with test coverage
- [ ] Audit events capture rejected and accepted proof validation outcomes

## References
- `backend/app/routes/goals.py`
- `backend/app/goal_types/registry.py`
- `backend/app/schemas/goal.py`
- `backend/app/models/proof.py`
- `backend/app/models/goal.py`
- `backend/app/main.py`
- PM tracker: `D083 enforce strict proof schema/state validation`

## Dev Agent Record
- Status: Not started
- Implementation notes: _TBD by Dev_
- Tests added/updated: _TBD by Dev_

## Senior Developer Review
- Review status: Pending
- Reviewer: _TBD_
- Review notes: _TBD_

## Review Follow-ups
- _None yet_
