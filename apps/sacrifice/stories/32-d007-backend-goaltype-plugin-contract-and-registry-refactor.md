# Story

## Title
D007 Backend GoalType plugin contract and registry refactor

## Description
Refactor backend goal-type handling to a pluggable `GoalType` package model under `backend/app/goal_types/`, replace `goal_type` branching in `backend/app/routes/goals.py` with registry dispatch, generate Celery includes from the registry, port the four existing goal types with no observable behavior change, add `GET /api/goal-types`, and add registry-discovery smoke coverage.

# Acceptance Criteria

- AC1: A new package `backend/app/goal_types/` exists, with each goal type as a self-contained sub-package containing at minimum:
  - `definition.py` — registers `name`, human-readable `description`, `sample_prompts` (list[str]) used by chat matching in D009, and `criteria_schema` (JSON schema for the goal's `criteria_data`).
  - `verifier.py` — verification entrypoint conforming to the abstract base interface.
  - `__init__.py` — exposes the definition so the registry can discover it.
- AC2: An abstract base class lives in `backend/app/goal_types/base.py`. All existing and future goal-type modules inherit from it. The exact interface (method signatures, lifecycle hooks) is designed by the Architect persona during chain execution; this direction does not pre-design it.
- AC3: A registry module (`backend/app/goal_types/registry.py`) discovers sub-packages at import time, validates each conforms to the base, and exposes `list_types()` and `get_type(name)`.
- AC4: All four existing goal types are ported with no observable behavior change: `youtube_video`, `api_endpoint`, `dev_sandbox`, `github_repo`. Every existing test in `backend/tests/` continues to pass after the port.
- AC5: `backend/app/routes/goals.py` no longer branches on `goal_type` for proof dispatch; it looks the type up in the registry and calls the registered verifier.
- AC6: The Celery `include` list in `backend/app/core/celery_app.py` is generated from the registry rather than hard-coded module paths.
- AC7: `GET /api/goal-types` returns the list of registered goal types (see `api_spec.md`).
- AC8: Registry-discovery smoke test: a stub goal-type package added at `backend/app/goal_types/_smoke/` (added then removed within the test) demonstrates the registry picks it up without any other file changes.

# Tasks / Subtasks

- [ ] T1 Create backend plugin package layout under `backend/app/goal_types/`.
  - [ ] T1.1 Add `backend/app/goal_types/base.py` for the abstract contract.
  - [ ] T1.2 Add `backend/app/goal_types/registry.py` for package discovery, validation, `list_types()`, and `get_type(name)`.
  - [ ] T1.3 Add one sub-package each for `youtube_video`, `api_endpoint`, `dev_sandbox`, and `github_repo` with `definition.py`, `verifier.py`, and `__init__.py`.
- [ ] T2 Port existing goal-type behavior into the new package model without observable behavior change.
  - [ ] T2.1 Preserve current verification behavior for `youtube_video`.
  - [ ] T2.2 Preserve current verification behavior for `api_endpoint`.
  - [ ] T2.3 Preserve current verification behavior for `dev_sandbox`.
  - [ ] T2.4 Preserve current verification behavior for `github_repo`.
- [ ] T3 Replace route-level `goal_type` branching in `backend/app/routes/goals.py` with registry-based dispatch.
- [ ] T4 Generate Celery include configuration from the registry in `backend/app/core/celery_app.py`.
- [ ] T5 Add `GET /api/goal-types` backend endpoint matching `api_spec.md`.
  - [ ] T5.1 Ensure authenticated behavior and response shape align with spec.
- [ ] T6 Add backend test coverage.
  - [ ] T6.1 Add/adjust tests for registry discovery and validation.
  - [ ] T6.2 Add/adjust tests for registry-backed proof dispatch.
  - [ ] T6.3 Add/adjust tests for `GET /api/goal-types` success and auth behavior.
  - [ ] T6.4 Add registry-discovery smoke test using temporary `backend/app/goal_types/_smoke/` package add/remove within the test.
- [ ] T7 Run and preserve existing backend test suite behavior.
  - [ ] T7.1 Confirm existing tests in `backend/tests/` continue to pass after the port.

# Dev Notes

## Direction anchors
- This story covers backend runtime architecture and backend API surface only.
- Frontend API client work is split to `stories/0-d007-frontend-api-client-support-for-goal-type-registry.md`.
- Context doc rewrites are split to `stories/0-d007-rewrite-backend-context-docs-for-goaltype-architecture.md`.

## flow.md (verbatim)

(none)

## api_spec.md (verbatim)

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

## Direction acceptance criteria (verbatim)

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

## Context pointers to load
- [Source: context/project.md#Identity]
- [Source: context/project.md#Stack]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on the backend API]
- [Source: context/navigation.md#When working on background verification, deadlines, or payments]
- [Source: context/navigation.md#When working on goals, proof submission, or verification status]

## Implementation constraints
- The Architect persona designs the exact abstract interface; do not guess beyond the direction contract.
- Preserve observable behavior for all four existing goal types.
- Keep frontend work out of this story except backend support for the endpoint consumed by frontend later.
- Do not rewrite context docs in this story.

## Suggested code touchpoints
- `backend/app/goal_types/`
- `backend/app/routes/goals.py`
- `backend/app/core/celery_app.py`
- `backend/app/schemas/`
- `backend/app/services/`
- `backend/app/workers/`
- `backend/tests/`

# References

- Direction: `direction.md`
- API spec: `api_spec.md`
- Future companion stories:
  - `stories/0-d007-frontend-api-client-support-for-goal-type-registry.md`
  - `stories/0-d007-rewrite-backend-context-docs-for-goaltype-architecture.md`

# Dev Agent Record

## Agent Model Used
TBD

## Debug Log References
- TBD

## Completion Notes List
- TBD

## File List
- TBD

# Senior Developer Review

- TBD

# Review Follow-ups

- TBD
