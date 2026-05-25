# Story

## Title
D007 Frontend API client support for goal type registry

## Description
As a frontend developer
I want the API client to expose `listGoalTypes()`
so that future frontend flows can consume the backend GoalType registry without additional client plumbing.

## Acceptance Criteria
- AC1: `frontend/services/api.ts` gains a `listGoalTypes()` call. No other frontend changes in this direction.
- AC2: `GET /api/goal-types` returns the list of registered goal types (see `api_spec.md`).
- AC3: No broader frontend UX changes in this direction beyond the API client method.

## Tasks / Subtasks
- [ ] Add `listGoalTypes()` to `frontend/services/api.ts`. (AC1, AC2)
  - [ ] Match the request method, path, and response shape from `api_spec.md`. (AC2)
  - [ ] Reuse existing API client auth/error handling patterns already present in `frontend/services/api.ts`. (AC1)
- [ ] Do not modify screens, hooks, navigation, or UX copy as part of this story. (AC1, AC3)
- [ ] Add or update focused client-level tests if this repository already covers `frontend/services/api.ts`; otherwise keep scope limited to the method implementation only. (AC1, AC3)
- [ ] Validate method contract against backend story output for `GET /api/goal-types`. (AC2)

## Dev Notes
### Direction flow.md (verbatim)
```md
(none)
```

### Direction api_spec.md (verbatim)
```md
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
```

### Context pointers to load
- [Source: context/project.md#Stack]
- [Source: context/navigation.md#When working on the Expo client]
- [Source: context/navigation.md#When working on goals, proof submission, or verification status]

### Direction acceptance criteria (verbatim)
```md
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
```

### Implementation constraints
- Frontend scope is intentionally narrow: `frontend/services/api.ts` only. No screen, navigation, or UX work belongs here. [PM result]
- Consume the backend contract exactly as specified in `api_spec.md`; do not invent alternative shapes. [Direction]
- Preserve existing frontend client conventions for base URL, auth, and response handling. [Source: context/project.md#Active constraints]

## References
- `frontend/services/api.ts`
- `frontend/hooks/useAuth.tsx`
- `frontend/screens/`
- `backend/app/routes/goals.py`

## Dev Agent Record
- Status: Not started
- Agent Model: TBD
- Debug Log References: TBD
- Completion Notes: TBD
- File List: TBD

## Senior Developer Review
- Status: Pending
- Reviewer: TBD
- Review Notes: TBD

## Review Follow-ups
- None yet.
