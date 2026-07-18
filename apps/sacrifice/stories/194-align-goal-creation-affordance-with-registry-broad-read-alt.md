# Story
**Title:** Align goal creation affordance with registry — broad read
**Slug:** align-goal-creation-affordance-with-registry-broad-read-alt
**Scope:** frontend

## Acceptance Criteria
- [ ] The set of selectable goal types in the UI matches the registry-backed set accepted by the API.
- [ ] Attempting to create a registered goal type does not fail because of stale client or schema unions.

### Testable Claims (EARS)
AC1.1: WHEN the goal creation UI loads selectable goal types, THE UI SHALL present the registry-backed set accepted by the API.
AC2.1: WHEN a user attempts to create a registered goal type, THE client submission path SHALL not fail because of stale client unions.
AC2.2: WHEN a user attempts to create a registered goal type, THE system SHALL not fail because of stale schema unions.

## Tasks / Subtasks
- [ ] Replace hardcoded goal-type option source in the goal creation surface with `/api/goal-types` data.
- [ ] Preserve selection and rendering behavior for registry-returned goal-type metadata already used by the screen.
- [ ] Remove fixed frontend create-goal type unions that reject registry-returned goal types.
- [ ] Update create-goal payload typing and form state to carry registry-returned goal type identifiers without hardcoded narrowing.
- [ ] Keep request construction aligned with the existing create-goal API contract consumed by the app.
- [ ] Add or update frontend tests for registry-backed option loading and create-goal submission with a registered non-hardcoded type.
- [ ] Verify no UI path still derives selectable goal types from stale local constants.
- [ ] Document any backend dependency or blocker discovered during implementation in Dev Agent Record.

## Dev Notes
- Scope for this broad-read story intentionally combines the PM decomposition's two frontend failure modes into one frontend delivery slice: discovery/affordance parity and stale frontend union removal. Backend schema relaxation remains out of scope for code changes in this story, but must be called out if it blocks end-to-end success.
- No `flow.md` provided by direction.
- No `api_spec.md` provided by direction.
- Load these context files before implementation and test design:
  - [Source: context/project.md#Identity]
  - [Source: context/project.md#Active constraints]
  - [Source: context/navigation.md#When working on mobile goal creation or proof UX]
  - [Source: context/navigation.md#When working on generated goal types or registry compatibility]
- Current-state pointers to inspect:
  - [Source: context/current-state.md#UNAVAILABLE-IN-PRELUDE]
  - [Source: context/modules/frontend.md#UNAVAILABLE-IN-PRELUDE]
  - [Source: context/modules/backend.md#UNAVAILABLE-IN-PRELUDE]
- Direction acceptance criteria verbatim:
  - [ ] The set of selectable goal types in the UI matches the registry-backed set accepted by the API.
  - [ ] Attempting to create a registered goal type does not fail because of stale client or schema unions.
- Current-state evidence from provided prelude:
  - Backend already exposes goal-type metadata at `/api/goal-types`.
  - Frontend `GoalCreateScreen` still builds options from hardcoded local constants instead of consuming that endpoint.
  - Goal creation is still fixed to `youtube_video`, `api_endpoint`, `dev_sandbox`, and `github_repo` in the client union.
  - Backend schema validation and database enums remain fixed to four built-in types; frontend work here must avoid reintroducing client-side blockers and must surface any remaining backend blocker clearly.
- Implementation boundary:
  - Do not add new goal-type business rules.
  - Do not change proof submission flows.
  - Do not invent fallback types beyond what `/api/goal-types` returns.
  - Do not hardcode a mirrored allowlist in new frontend code.
- Test design focus:
  - UI option list sourced from API response, not local constants.
  - Selection state and submission payload accept a registry-returned type outside the prior fixed union.
  - Failure analysis distinguishes frontend type rejection from any backend schema rejection.

## References
- `frontend/screens/GoalCreateScreen.tsx`
- `frontend/services/api.ts`
- `backend/app/routes/goals.py`
- `backend/app/schemas/goal.py`
- `backend/app/models/goal.py`
- `backend/app/goal_types/registry.py`
- `frontend/App.tsx`

## Dev Agent Record
- Agent Model:
- Debug Log References:
- Completion Notes:
- File List:

## Senior Developer Review
- Reviewer:
- Review Date:
- Verdict:
- Notes:

## Review Follow-ups
- [ ] None recorded yet.
