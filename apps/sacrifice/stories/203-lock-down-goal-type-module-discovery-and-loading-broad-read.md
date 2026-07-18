# Story

## Title
Lock down goal-type module discovery and loading — broad read

## Summary
Harden backend goal-type module loading end-to-end: enforce trusted-path and allowlist discovery, fail application startup when integrity or interface validation fails, and emit security logging for module load decisions plus verifier exception paths.

## Scope
backend

## Acceptance Criteria
- [ ] Goal-type registry only loads allowlisted modules from trusted paths
- [ ] Startup fails if module integrity checks or interface validation fail
- [ ] Security logging records module load decisions and verifier exceptions

### Testable Claims (EARS)
AC1.1: WHEN the goal-type registry discovers candidate modules, THE goal-type registry SHALL load only allowlisted modules from trusted paths
AC2.1: WHEN application startup runs module integrity checks, THE application startup SHALL fail if the checks fail
AC2.2: WHEN application startup validates a goal-type module interface, THE application startup SHALL fail if the interface validation fails
AC3.1: WHEN the goal-type registry makes a module load decision, THE security logging component SHALL record the module load decision
AC3.2: WHEN a goal-type verifier raises an exception, THE security logging component SHALL record the verifier exception

## Tasks / Subtasks
- [ ] Define repo-local trusted-path contract for goal-type discovery
- [ ] Define repo-local allowlist source of truth for loadable goal-type modules
- [ ] Update `backend/app/goal_types/registry.py` discovery to enforce trusted paths before import
- [ ] Update `backend/app/goal_types/registry.py` loading to enforce allowlist membership before import
- [ ] Add integrity-check hook for discovered modules
- [ ] Add interface validation for loaded goal-type modules
- [ ] Wire application startup to execute discovery, integrity, and interface validation fail-fast
- [ ] Ensure startup raises deterministic failure on integrity validation errors
- [ ] Ensure startup raises deterministic failure on interface validation errors
- [ ] Add structured security log events for allow/deny module load decisions
- [ ] Add structured security log events for verifier exception handling
- [ ] Confirm logs omit proof payload contents and other unsafe detail
- [ ] Add backend tests for trusted-path allow/deny cases
- [ ] Add backend tests for allowlist allow/deny cases
- [ ] Add backend tests for startup failure on integrity-check failures
- [ ] Add backend tests for startup failure on interface validation failures
- [ ] Add backend tests asserting security log emission for module load decisions
- [ ] Add backend tests asserting security log emission for verifier exceptions

## Dev Notes
- Broad-read scope covers all three direction acceptance criteria in one backend story because this invocation targets the broad-read record rather than PM child-story granularity.
- `flow.md` is absent in the direction.
- `api_spec.md` is absent in the direction.
- Direction acceptance criteria (verbatim embed):
  - [ ] Goal-type registry only loads allowlisted modules from trusted paths
  - [ ] Startup fails if module integrity checks or interface validation fail
  - [ ] Security logging records module load decisions and verifier exceptions
- Implementation boundary: keep the allowlist and trusted-path policy repo-local and minimal; do not invent new product-facing configuration surfaces beyond what is needed to satisfy the direction.
- Logging boundary: security-relevant, structured, and free of sensitive proof payload data.
- Candidate code areas to inspect:
  - `backend/app/goal_types/registry.py`
  - `backend/app/routes/goals.py`
  - `backend/app/main.py`
  - `backend/app/config.py`
  - `backend/app/schemas/goal.py`
  - `backend/app/models/goal.py`
- Context pointers available in this invocation:
  - [Source: context/project.md#Identity]
  - [Source: context/project.md#Stack]
  - [Source: context/project.md#Top-level layout]
  - [Source: context/project.md#Active constraints]
  - [Source: context/navigation.md#When working on backend API or goal lifecycle]
  - [Source: context/navigation.md#When working on generated goal types or registry compatibility]
- Missing canonical files referenced by navigation prelude were not provided in this invocation: `context/current-state.md`, `context/modules/backend.md`, `context/glossary.md`, `context/modules/frontend.md`, `context/architecture-diagrams.md`.

## References
- `PRD.md`
- `backend/app/goal_types/registry.py`
- `backend/app/routes/goals.py`
- `backend/app/main.py`
- `backend/app/config.py`
- `backend/app/schemas/goal.py`
- `backend/app/models/goal.py`
- `backend/tests/`

## Dev Agent Record
- Status: Not started
- Agent: TBD
- Branch: TBD
- Notes:
  - Maintain TDD sequencing.
  - Keep startup failure deterministic and testable.
  - Keep security logs structured and sanitized.

## Senior Developer Review
- Status: Pending
- Reviewer: TBD
- Checklist:
  - [ ] Trusted-path enforcement verified
  - [ ] Allowlist enforcement verified
  - [ ] Integrity failure blocks startup
  - [ ] Interface validation failure blocks startup
  - [ ] Security logging emitted for allow/deny load decisions
  - [ ] Security logging emitted for verifier exceptions
  - [ ] No sensitive proof payload details logged
  - [ ] Tests cover positive and negative paths

## Review Follow-ups
- None yet
