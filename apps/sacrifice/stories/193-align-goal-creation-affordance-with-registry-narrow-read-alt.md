# Story

## Title
Align goal creation affordance with registry — narrow read

## Story
**As a** user creating a goal,
**I want** the GoalCreate screen to load its selectable goal types from the backend registry endpoint,
**so that** the UI advertises the same goal-type set the API already exposes.

## Scope
Frontend-only narrow read. Replace hardcoded goal-type option discovery in the goal creation affordance with `/api/goal-types` data. Do not expand this story into create-payload union removal or backend schema/model changes beyond what is required to render and select registry-backed options in the UI.

## Acceptance Criteria
- [ ] The set of selectable goal types in the UI matches the registry-backed set accepted by the API.
- [ ] Attempting to create a registered goal type does not fail because of stale client or schema unions.

### Testable Claims (EARS)
AC1.1: WHEN the GoalCreate UI loads selectable goal types, THE GoalCreate UI SHALL present the registry-backed set accepted by the API
AC2.1: UNTESTABLE-AS-WRITTEN — this frontend-narrow story does not own client or schema union removal end-to-end; the criterion spans separate frontend and backend slices and lacks a story-local observable boundary

## Tasks / Subtasks
- [ ] Replace hardcoded GoalCreate goal-type option source with `/api/goal-types` fetch
- [ ] Add/adjust frontend API helper for goal-type metadata retrieval
- [ ] Map returned registry metadata into GoalCreate selectable option state
- [ ] Preserve current selection UX for currently supported types when registry data loads
- [ ] Handle loading state for goal-type option retrieval
- [ ] Handle fetch failure state without silently showing stale hardcoded options
- [ ] Remove local hardcoded option list from GoalCreate selection rendering path
- [ ] Keep downstream form behavior scoped to existing create flow inputs
- [ ] Add/update frontend tests covering registry-backed option rendering
- [ ] Add/update frontend tests covering failure/loading behavior for option discovery

## Dev Notes
- No `flow.md` provided in direction.
- No `api_spec.md` provided in direction.

### Direction acceptance criteria (verbatim)
- [ ] The set of selectable goal types in the UI matches the registry-backed set accepted by the API.
- [ ] Attempting to create a registered goal type does not fail because of stale client or schema unions.

### Scope guardrails
- This is the narrow-read/frontend affordance slice.
- Implement discovery parity in the GoalCreate selection surface.
- Do not solve stale create-goal payload unions in this story unless a minimal typing adjustment is strictly required for the selection UI to compile.
- Do not change backend schema validation, DB enums, or registry semantics in this story.
- If registry-backed options expose metadata beyond what current GoalCreate renders, prefer non-blocking display using existing fields already surfaced by `/api/goal-types` rather than introducing new UX requirements.

### Context pointers
- [Source: context/project.md#Identity]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on mobile goal creation or proof UX]
- [Source: context/current-state.md#Goal-type creation remains hardcoded across frontend and backend]
- [Source: context/current-state.md#Backend already serves registry metadata for goal types]
- [Source: context/modules/frontend.md#Goal creation screen]
- [Source: context/modules/frontend.md#API service layer]
- [Source: context/modules/backend.md#Goal type registry and metadata endpoint]

### Current-state anchors from prelude
- Backend already exposes goal-type metadata at `/api/goal-types`.
- Frontend `GoalCreateScreen` still builds options from hardcoded local constants.
- Goal creation is still fixed to `youtube_video`, `api_endpoint`, `dev_sandbox`, and `github_repo` in the client union, backend schema validation, and database enums.
- Frontend work should follow Expo docs version `https://docs.expo.dev/versions/v54.0.0/`.

## References
- `frontend/screens/GoalCreateScreen.tsx`
- `frontend/services/api.ts`
- `backend/app/routes/goals.py`
- `backend/app/goal_types/registry.py`
- `backend/app/schemas/goal.py`
- `backend/app/models/goal.py`

## Dev Agent Record
- Status: Not started
- Agent: TBD
- Branch: TBD
- Notes:
  - TBD

## Senior Developer Review
- Status: Pending
- Reviewer: TBD
- Review notes:
  - TBD

## Review Follow-ups
- None yet
