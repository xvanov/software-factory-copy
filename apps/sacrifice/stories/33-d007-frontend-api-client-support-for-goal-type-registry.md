# Story

## Title
D007 Frontend API client support for goal type registry

## Scope
frontend

## Summary
Add `listGoalTypes()` to `frontend/services/api.ts` for the new backend registry endpoint. No other frontend UX or screen changes are included in this direction.

# Acceptance Criteria

- AC1: `frontend/services/api.ts` gains a `listGoalTypes()` call. No other frontend changes in this direction.

# Tasks / Subtasks

- [ ] T1 Add `listGoalTypes()` to `frontend/services/api.ts`. (AC1)
  - [ ] T1.1 Call `GET /api/goal-types` per `api_spec.md`. (AC1)
  - [ ] T1.2 Model the response shape returned by the endpoint: `goal_types[]` with `name`, `description`, `sample_prompts`, and `criteria_schema`. (AC1)
  - [ ] T1.3 Preserve existing auth/request conventions already used by the frontend API client. (AC1)
- [ ] T2 Confirm no other frontend files are changed in this story. (AC1)

# Dev Notes

## Direction flow.md

```text
(none)
```

## Direction api_spec.md

```markdown
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

## Context pointers to load

- [Source: context/project.md#Identity]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on the Expo client]
- [Source: context/navigation.md#When working on goal creation]

## Direction acceptance criteria (verbatim)

```markdown
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

## Constraints / handoff notes

- This story is intentionally limited to `frontend/services/api.ts` only.
- No screen, navigation, or UX changes are allowed in this direction.
- Match existing frontend API/auth conventions already present in the repo.

# References

- `frontend/services/api.ts`
- `frontend/AGENTS.md`

# Dev Agent Record

## Status
Not started

## Notes
- Reserved for implementation agent.

# Senior Developer Review

## Status
Pending

## Notes
- Reserved for senior developer review.

# Review Follow-ups

- None yet.
