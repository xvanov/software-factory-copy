# Story

## Title
D007 Backend GoalType plugin contract and registry refactor

## Scope
backend

## Summary
Implement the pluggable GoalType package model in the backend by adding the GoalType base and registry, porting the four existing goal types, replacing route and Celery hard-coding with registry-driven dispatch, and exposing `GET /api/goal-types`.

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
- AC8: `frontend/services/api.ts` gains a `listGoalTypes()` call. No other frontend changes in this direction.
- AC9: Registry-discovery smoke test: a stub goal-type package added at `backend/app/goal_types/_smoke/` (added then removed within the test) demonstrates the registry picks it up without any other file changes.
- AC10: `context/modules/backend-app.md` and `context/modules/backend-workers.md` are rewritten to reflect the new layout (no historical "previously branched on goal_type" notes — current truth only).

# Tasks / Subtasks

- [ ] T1 Create backend GoalType package structure under `backend/app/goal_types/`.
  - [ ] T1.1 Add `backend/app/goal_types/base.py` for the abstract GoalType contract.
  - [ ] T1.2 Add `backend/app/goal_types/registry.py` with discovery, validation, `list_types()`, and `get_type(name)`.
  - [ ] T1.3 Ensure package discovery works from sub-package `__init__.py` exports.
- [ ] T2 Port existing goal types into self-contained packages with no observable behavior change.
  - [ ] T2.1 Port `youtube_video`.
  - [ ] T2.2 Port `api_endpoint`.
  - [ ] T2.3 Port `dev_sandbox`.
  - [ ] T2.4 Port `github_repo`.
  - [ ] T2.5 Preserve current verification behavior and existing backend test expectations.
- [ ] T3 Replace goal-type branching in `backend/app/routes/goals.py` with registry dispatch.
  - [ ] T3.1 Look up the requested/associated goal type through `get_type(name)`.
  - [ ] T3.2 Call the registered verifier through the Architect-defined interface.
- [ ] T4 Replace hard-coded Celery includes with registry-derived includes in `backend/app/core/celery_app.py`.
- [ ] T5 Add backend API support for `GET /api/goal-types` per `api_spec.md`.
  - [ ] T5.1 Return registered goal types with `name`, `description`, `sample_prompts`, and `criteria_schema`.
  - [ ] T5.2 Preserve auth behavior consistent with the API spec.
- [ ] T6 Add backend test coverage.
  - [ ] T6.1 Add registry validation/discovery coverage.
  - [ ] T6.2 Add `GET /api/goal-types` response coverage.
  - [ ] T6.3 Add smoke coverage proving a temporary `backend/app/goal_types/_smoke/` package is discovered without any other file changes.
  - [ ] T6.4 Run/maintain all existing backend tests in `backend/tests/`.
- [ ] T7 Do not implement broader frontend work here beyond what this backend story must expose for the API contract.
- [ ] T8 Leave context module rewrites to the docs story; backend code must land in a shape those docs can describe as current truth.

# Dev Notes

## Direction flow.md (verbatim)

(none)

## Direction api_spec.md (verbatim)

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
- [Source: context/project.md#Top-level layout]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on the backend API]
- [Source: context/navigation.md#When working on background verification, deadlines, or payments]
- [Source: context/navigation.md#When working on goals, proof submission, or verification status]

## Implementation constraints

- Architect defines the exact abstract interface; do not guess beyond what the direction requires.
- Behavior preservation is mandatory; this is a refactor, not a product behavior rewrite.
- `frontend/services/api.ts` work belongs to the frontend story except where backend contract stability must be honored.
- Context module rewrites belong to the docs story.

# References

- Direction: `direction.md`
- API spec: `api_spec.md`
- Backend targets named in direction:
  - `backend/app/goal_types/`
  - `backend/app/goal_types/base.py`
  - `backend/app/goal_types/registry.py`
  - `backend/app/routes/goals.py`
  - `backend/app/core/celery_app.py`
  - `backend/tests/`

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

- Pending

# Review Follow-ups

- Pending
