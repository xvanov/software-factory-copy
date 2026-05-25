# Story

## Title
D007 Backend GoalType plugin contract and registry refactor

## Description
Refactor backend goal-type handling from hard-coded branching and static worker includes to a pluggable GoalType package model under `backend/app/goal_types/`. Port the four existing goal types without observable behavior change, add registry-backed `GET /api/goal-types`, and add registry-discovery smoke coverage.

## Acceptance Criteria
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
- AC9: `context/modules/backend-app.md` and `context/modules/backend-workers.md` are rewritten to reflect the new layout (no historical "previously branched on goal_type" notes — current truth only).

## Tasks / Subtasks
- [ ] Create `backend/app/goal_types/` package and goal-type sub-package layout for `youtube_video`, `api_endpoint`, `dev_sandbox`, and `github_repo`. (AC1, AC4)
  - [ ] Add per-type `definition.py`, `verifier.py`, and `__init__.py`. (AC1)
  - [ ] Preserve current runtime behavior during the port. (AC4)
- [ ] Add `backend/app/goal_types/base.py` with the abstract contract selected during Architect execution. (AC2)
- [ ] Add `backend/app/goal_types/registry.py` with discovery, validation, `list_types()`, and `get_type(name)`. (AC3)
- [ ] Refactor `backend/app/routes/goals.py` to dispatch proof verification through the registry instead of `goal_type` branching. (AC5)
- [ ] Refactor `backend/app/core/celery_app.py` to derive Celery includes from the registry. (AC6)
- [ ] Add `GET /api/goal-types` backend API implementation matching `api_spec.md`. (AC7)
- [ ] Add/adjust backend tests to cover registry discovery, endpoint response, and behavior preservation. (AC4, AC7, AC8)
  - [ ] Add smoke test that creates `backend/app/goal_types/_smoke/` temporarily and proves discovery without other file edits. (AC8)
  - [ ] Ensure existing backend test suite continues to pass. (AC4)
- [ ] Coordinate with docs story for context rewrites; backend code should land in a shape that matches rewritten current-truth docs. (AC9)

## Dev Notes
### Direction acceptance criteria (verbatim embed)
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

### flow.md (verbatim embed)
(none)

### api_spec.md (verbatim embed)
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
- [Source: context/project.md#Identity]
- [Source: context/project.md#Stack]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on the backend API]
- [Source: context/navigation.md#When working on background verification, deadlines, or payments]
- [Source: context/navigation.md#When working on goals, proof submission, or verification status]

### Implementation notes
- Architect will define the exact abstract interface; do not guess beyond the direction and resulting architecture truth.
- Behavior preservation is mandatory. This is a refactor enabling extensibility, not a user-facing rewrite.
- The smoke test must prove registry discovery works without additional manual wiring.
- `AC9` is included here because it remains part of the direction, but the actual doc rewrite is split into a separate docs story.

## References
- Direction: `direction.md`
- API spec: `api_spec.md`
- Backend runtime targets: `backend/app/routes/goals.py`, `backend/app/core/celery_app.py`, `backend/app/workers/`, `backend/tests/`
- Goal type package target: `backend/app/goal_types/`

## Dev Agent Record
- Status: Not started
- Notes: Await Architect-updated current-state truth before implementation details diverge from the direction.

## Senior Developer Review
- Pending

## Review Follow-ups
- None yet
