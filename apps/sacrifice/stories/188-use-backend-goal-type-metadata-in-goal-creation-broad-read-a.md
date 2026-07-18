# Story
**Title:** Use backend goal-type metadata in goal creation — broad read
**Slug:** use-backend-goal-type-metadata-in-goal-creation-broad-read-a
**Scope:** frontend
**Target:** `stories/0-use-backend-goal-type-metadata-in-goal-creation-broad-read-a.md`

## Acceptance Criteria
- [ ] Goal creation fetches and renders options from /api/goal-types instead of local constants.
- [ ] A backend-registered goal type appears in the picker without changing frontend source lists.

### Testable Claims (EARS)
AC1.1: WHEN GoalCreateScreen prepares the goal-type picker, THE frontend SHALL fetch goal-type options from `/api/goal-types` instead of local constants
AC1.2: WHEN `/api/goal-types` returns goal-type metadata, THE goal-type picker SHALL render its options from that response
AC2.1: WHEN the backend returns a registered goal type in `/api/goal-types`, THE goal-type picker SHALL include that goal type without requiring changes to frontend source lists

## Tasks / Subtasks
- [ ] Replace GoalCreateScreen goal-type option source with `/api/goal-types` response
- [ ] Add frontend API helper or reuse existing service path for `/api/goal-types`
- [ ] Preserve create-flow behavior for the four built-in goal types while switching option source
- [ ] Render picker labels/details from backend metadata fields already exposed by the endpoint
- [ ] Remove dependency on hardcoded local goal-type option constants for picker population
- [ ] Handle loading and error states without blocking existing screen initialization
- [ ] Verify selected backend-provided goal type still flows into create-goal submission payload
- [ ] Update or add focused frontend test coverage for metadata-driven rendering if implemented in this slice

## Dev Notes
- No `flow.md` provided by direction.
- No `api_spec.md` provided by direction.

### Context pointers
- [Source: context/project.md#Identity]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on mobile goal creation or proof UX]
- [Source: context/current-state.md#UNAVAILABLE IN PRELUDE]
- [Source: context/modules/frontend.md#UNAVAILABLE IN PRELUDE]

### Direction acceptance criteria (verbatim)
- [ ] Goal creation fetches and renders options from /api/goal-types instead of local constants.
- [ ] A backend-registered goal type appears in the picker without changing frontend source lists.

### Implementation constraints
- Backend endpoint already exists at `/api/goal-types` and returns `name`, `description`, `sample_prompts`, and `criteria_schema`.
- Story scope is frontend-only; do not require backend contract changes.
- Preserve existing goal creation behavior for the four built-in goal types while changing picker population to backend metadata.
- Frontend work must align with Expo SDK 54 repo guidance.
- Current frontend hardcodes goal-type unions/options in `frontend/screens/GoalCreateScreen.tsx`; this story replaces picker sourcing, not the broader proof/upload path.

### Likely touchpoints
- `frontend/screens/GoalCreateScreen.tsx`
- `frontend/services/api.ts`
- Existing frontend test file covering goal creation screen behavior

## References
- `frontend/screens/GoalCreateScreen.tsx`
- `frontend/services/api.ts`
- `backend/app/routes/goals.py`
- `backend/app/goal_types/registry.py`
- `backend/app/schemas/goal.py`
- `backend/app/models/goal.py`
- `context/project.md`
- `context/navigation.md`

## Dev Agent Record
- Status: Not started
- Implementation notes: _TBD by Dev_
- Files changed: _TBD by Dev_

## Senior Developer Review
- Status: Pending
- Reviewer notes: _TBD_

## Review Follow-ups
- None yet
