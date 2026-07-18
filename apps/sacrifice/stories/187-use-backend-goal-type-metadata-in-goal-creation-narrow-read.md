# Story

## Story
As a mobile user creating a goal,
I want GoalCreateScreen to load goal-type choices from the backend metadata endpoint,
so that any backend-supported goal type can appear in the picker without frontend source-list edits.

## Acceptance Criteria
- [ ] Goal creation fetches and renders options from /api/goal-types instead of local constants.
- [ ] A backend-registered goal type appears in the picker without changing frontend source lists.

### Testable Claims (EARS)
AC1.1: WHEN GoalCreateScreen loads the goal-type picker, THE frontend SHALL fetch goal-type options from /api/goal-types instead of local constants
AC1.2: WHEN /api/goal-types returns goal-type options, THE goal-type picker SHALL render those returned options
AC2.1: WHEN the backend returns a registered goal type, THE goal-type picker SHALL display that goal type without requiring changes to frontend source lists

## Tasks / Subtasks
- [ ] Replace GoalCreateScreen hardcoded goal-type option source with /api/goal-types data
- [ ] Add frontend API helper usage for loading goal-type metadata into the create flow
- [ ] Map backend fields needed for picker rendering within GoalCreateScreen
- [ ] Preserve current create-flow behavior for the four built-in goal types while changing the picker data source
- [ ] Handle initial loading state for goal-type metadata retrieval in GoalCreateScreen
- [ ] Handle fetch failure without crashing the create screen
- [ ] Remove or bypass local picker constants as the source of truth for selectable goal types
- [ ] Confirm selected backend-provided goal type still flows through existing goal creation submission path

## Dev Notes
- Scope boundary: narrow read. This story covers frontend wiring for GoalCreateScreen picker data source only. Do not expand into backend schema, database enum, proof submission, camera/upload, or unrelated UX rewrites.
- No `flow.md` content provided by direction.
- [api_spec.md: see no backend story in this direction; none provided]
- Direction acceptance criteria verbatim:
  - [ ] Goal creation fetches and renders options from /api/goal-types instead of local constants.
  - [ ] A backend-registered goal type appears in the picker without changing frontend source lists.
- Current-state implementation constraints to respect:
  - `/api/goal-types` already exists and returns `name`, `description`, `sample_prompts`, and `criteria_schema`.
  - Goal creation UI currently builds options from hardcoded local constants.
  - Existing create behavior for `youtube_video`, `api_endpoint`, `dev_sandbox`, and `github_repo` must remain functional after switching picker sourcing.
- Load these context files before implementation and test design:
  - [Source: context/project.md#Identity]
  - [Source: context/project.md#Active constraints]
  - [Source: context/navigation.md#When working on mobile goal creation or proof UX]
  - [Source: context/current-state.md#Goal-type metadata is available in the backend but not consumed by the mobile create flow]
  - [Source: context/modules/frontend.md#Goal creation]
  - [Source: context/modules/frontend.md#API integration]
  - [Source: context/modules/backend.md#Goal type registry and metadata]
- Likely code touchpoints from current context:
  - `frontend/screens/GoalCreateScreen.tsx`
  - `frontend/services/api.ts`
- Implementation notes for downstream Dev/Test:
  - Treat backend metadata as the canonical picker source.
  - Do not add frontend-maintained fallback lists as a parallel source of truth.
  - Rendering may use backend-provided `name` and supporting metadata already exposed by the endpoint.
  - Keep the create submission contract compatible with existing backend-supported built-in types.

## References
- `frontend/screens/GoalCreateScreen.tsx`
- `frontend/services/api.ts`
- `backend/app/routes/goals.py`
- `backend/app/goal_types/registry.py`
- `backend/app/schemas/goal.py`
- `backend/app/models/goal.py`

## Dev Agent Record
- Status: Not started
- Agent Model: 
- Branch: 
- PR: 
- Notes: 

## Senior Developer Review
- Review Status: Pending
- Reviewer: 
- Review Notes: 

## Review Follow-ups
- None yet
