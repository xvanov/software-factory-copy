# Story

## Title
D007 Frontend API client support for goal type registry

## Summary
Add `listGoalTypes()` to `frontend/services/api.ts` so the frontend can consume the new backend goal type registry endpoint. No screen, navigation, or UX changes are included in this story.

## Scope
- Add a typed API client method for `GET /api/goal-types`.
- Keep the change isolated to `frontend/services/api.ts` unless a colocated type declaration in the same client module is required.
- Do not modify screens, navigation, hooks, or UI behavior.

## Acceptance Criteria
1. A new package `backend/app/goal_types/` exists, with each goal type as a self-contained sub-package containing at minimum:
   - `definition.py` — registers `name`, human-readable `description`, `sample_prompts` (list[str]) used by chat matching in D009, and `criteria_schema` (JSON schema for the goal's `criteria_data`).
   - `verifier.py` — verification entrypoint conforming to the abstract base interface.
   - `__init__.py` — exposes the definition so the registry can discover it.
2. An abstract base class lives in `backend/app/goal_types/base.py`. All existing and future goal-type modules inherit from it. The exact interface (method signatures, lifecycle hooks) is designed by the Architect persona during chain execution; this direction does not pre-design it.
3. A registry module (`backend/app/goal_types/registry.py`) discovers sub-packages at import time, validates each conforms to the base, and exposes `list_types()` and `get_type(name)`.
4. All four existing goal types are ported with no observable behavior change: `youtube_video`, `api_endpoint`, `dev_sandbox`, `github_repo`. Every existing test in `backend/tests/` continues to pass after the port.
5. `backend/app/routes/goals.py` no longer branches on `goal_type` for proof dispatch; it looks the type up in the registry and calls the registered verifier.
6. The Celery `include` list in `backend/app/core/celery_app.py` is generated from the registry rather than hard-coded module paths.
7. `GET /api/goal-types` returns the list of registered goal types (see `api_spec.md`).
8. `frontend/services/api.ts` gains a `listGoalTypes()` call. No other frontend changes in this direction.
9. Registry-discovery smoke test: a stub goal-type package added at `backend/app/goal_types/_smoke/` (added then removed within the test) demonstrates the registry picks it up without any other file changes.
10. `context/modules/backend-app.md` and `context/modules/backend-workers.md` are rewritten to reflect the new layout (no historical "previously branched on goal_type" notes — current truth only).
11. The Architect persona designs the exact abstract interface; this direction does not pre-design method signatures.
12. Existing backend tests must continue to pass after the port.
13. No broader frontend UX changes in this direction beyond the API client method.

## Tasks / Subtasks
- [ ] Inspect `frontend/services/api.ts` client patterns for authenticated GET requests and response typing.
- [ ] Add `listGoalTypes()` to `frontend/services/api.ts` targeting `GET /api/goal-types`.
- [ ] Model the response shape to include `goal_types[]` entries with `name`, `description`, `sample_prompts`, and `criteria_schema`.
- [ ] Keep the change limited to the API client surface; do not modify screens, hooks, navigation, or other frontend files unless strictly necessary for shared type placement inside the same client module.
- [ ] Verify the client method aligns with backend auth expectations and existing API helper conventions.

## Dev Notes
### Direction acceptance criteria (verbatim)
- A new package `backend/app/goal_types/` exists, with each goal type as a self-contained sub-package containing at minimum:
  - `definition.py` — registers `name`, human-readable `description`, `sample_prompts` (list[str]) used by chat matching in D009, and `criteria_schema` (JSON schema for the goal's `criteria_data`).
  - `verifier.py` — verification entrypoint conforming to the abstract base interface.
  - `__init__.py` — exposes the definition so the registry can discover it.
- An abstract base class lives in `backend/app/goal_types/base.py`. All existing and future goal-type modules inherit from it. The exact interface (method signatures, lifecycle hooks) is designed by the Architect persona during chain execution; this direction does not pre-design it.
- A registry module (`backend/app/goal_types/registry.py`) discovers sub-packages at import time, validates each conforms to the base, and exposes `list_types()` and `get_type(name)`.
- All four existing goal types are ported with no observable behavior change: `youtube_video`, `api_endpoint`, `dev_sandbox`, `github_repo`. Every existing test in `backend/tests/` continues to pass after the port.
- `backend/app/routes/goals.py` no longer branches on `goal_type` for proof dispatch; it looks the type up in the registry and calls the registered verifier.
- The Celery `include` list in `backend/app/core/celery_app.py` is generated from the registry rather than hard-coded module paths.
- `GET /api/goal-types` returns the list of registered goal types (see `api_spec.md`).
- `frontend/services/api.ts` gains a `listGoalTypes()` call. No other frontend changes in this direction.
- Registry-discovery smoke test: a stub goal-type package added at `backend/app/goal_types/_smoke/` (added then removed within the test) demonstrates the registry picks it up without any other file changes.
- `context/modules/backend-app.md` and `context/modules/backend-workers.md` are rewritten to reflect the new layout (no historical "previously branched on goal_type" notes — current truth only).

### flow.md (verbatim)
(none)

### api_spec.md (verbatim)
# API spec

## Endpoints

### `GET /api/goal-types`

- **Method:** GET
- **Path:** `/api/goal-types`
- **Request body:** `(none)`
- **Response body (success):**
  ```json
  {
    "goal_types": [
      {
        "name": "youtube_video",
        "description": "User uploads a video to YouTube; the system fetches the transcript and an LLM judges whether the content matches the goal description.",
        "sample_prompts": [
          "Post a YouTube walkthrough of my project by Friday",
          "Record a 5-minute video explaining my refactor"
        ],
        "criteria_schema": {
          "type": "object",
          "properties": {
            "min_duration_seconds": {"type": "integer"},
            "video_description": {"type": "string"}
          },
          "required": ["min_duration_seconds", "video_description"]
        }
      }
    ]
  }
  ```
- **Success status:** `200`
- **Error statuses:**
  - `401` — unauthenticated

### Context pointers to load
- [Source: context/project.md#Stack]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on the Expo client]

### Implementation notes
- This story is intentionally narrow. The PM direction explicitly limits frontend work to `frontend/services/api.ts` client support.
- Do not introduce frontend UX changes, screen integration, or navigation updates in this story.
- Match existing client conventions for base URL usage and auth handling.

## References
- `frontend/services/api.ts`
- `frontend/hooks/useAuth.tsx`
- `frontend/screens/`

## Dev Agent Record
- Agent Model Used: 
- Debug Log References: 
- Completion Notes: 
- File List: 

## Senior Developer Review
- Review Status: Pending
- Reviewer: 
- Review Notes: 

## Review Follow-ups
- [ ] None yet.
