# Story

## Title
D007 Frontend API client support for goal type registry

## Scope
frontend

## Summary
Add `listGoalTypes()` to `frontend/services/api.ts` for the new backend registry endpoint. No other frontend UX or screen changes are in scope for this direction.

# Acceptance Criteria

- AC1: `frontend/services/api.ts` gains a `listGoalTypes()` call. No other frontend changes in this direction.
- AC2: The Architect persona designs the exact abstract interface; this direction does not pre-design method signatures.

# Tasks / Subtasks

- [ ] Add `listGoalTypes()` to `frontend/services/api.ts` targeting `GET /api/goal-types`. (AC1)
- [ ] Align request/response handling with `api_spec.md`. (AC1)
- [ ] Keep all other frontend files unchanged unless required for type-safe compilation within `frontend/services/api.ts`. (AC1)
- [ ] Do not add screens, navigation changes, or UX changes in this story. (AC1)

# Dev Notes

## Direction flow.md

(none)

## Direction api_spec.md

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

## Context pointers

- [Source: context/project.md#Identity]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on the Expo client]
- [Source: context/navigation.md#When working on goal creation]
- [Source: context/navigation.md#When working on chat or goal-type matching]
- [Source: context/modules/frontend.md#Frontend]
- [Source: context/modules/goal-creation.md#Goal Creation]
- [Source: context/modules/chat.md#Chat]
- [Source: context/current-state.md#current-state]

## Verbatim direction acceptance criteria

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

## Notes for downstream agents

- Scope is intentionally narrow: client surface only in `frontend/services/api.ts`.
- Do not alter creation flow UX, screen navigation, or typed goal selection in this direction.
- If frontend tests/types require shaping interfaces for the response body, keep those changes strictly in support of `frontend/services/api.ts`.

# References

- `frontend/services/api.ts`
- `frontend/AGENTS.md`
- `context/modules/frontend.md`
- `context/modules/goal-creation.md`

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

## Reviewer

TBD

## Review Notes

- TBD

# Review Follow-ups

- [ ] TBD
