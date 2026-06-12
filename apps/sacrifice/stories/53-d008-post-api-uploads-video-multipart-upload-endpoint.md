# Story

## Title
D008 POST /api/uploads/video multipart upload endpoint

## Scope
backend

## Goal
Implement authenticated multipart video upload at `POST /api/uploads/video` on top of the uploads service, with thin route logic and response behavior matching `api_spec.md`.

# Acceptance Criteria

- A new backend route `POST /api/uploads/video` accepts multipart uploads. See `api_spec.md`.
- `backend/app/services/uploads.py` encapsulates path resolution, write, hash computation, and metadata persistence. The route is thin; the service is unit-testable.

# Tasks / Subtasks

- [ ] Add authenticated route `POST /api/uploads/video`.
- [ ] Accept `multipart/form-data` with `file`, `duration_seconds`, and optional `goal_id`.
- [ ] Restrict accepted media types to `video/mp4` and `video/quicktime` per `api_spec.md`.
- [ ] Enforce authenticated ownership checks when `goal_id` is provided.
- [ ] Return `403` when `goal_id` is provided but not owned by the authenticated user.
- [ ] Delegate write/path/hash/persistence behavior to `backend/app/services/uploads.py`.
- [ ] Return success response body and `201` status exactly as specified.
- [ ] Return `401`, `413`, `415`, and `422` behaviors per `api_spec.md` where applicable.
- [ ] Keep route logic thin; do not duplicate service logic inside the route.

# Dev Notes

- **Operator note (2026-06-12) — build on the merged foundations:** the
  `media_uploads` model (`app/models/media.py`, with `media_storage_path()`)
  and the uploads service (`app/services/uploads.py`) are ALREADY ON MAIN
  (stories 51/52). This story adds ONLY the POST route, wired to that
  existing service — do NOT recreate the model, service, config keys, or
  migrations. The orphan path segment is the literal `orphan`; the media root
  setting is `sacrifice_media_dir`.
- **Operator note — error contract:** the spec's error set for this endpoint
  is closed (401/403/413/415/422). A syntactically valid but NONEXISTENT
  `goal_id` returns 403 (treated as not-owned; avoids leaking goal ids) — not
  404.

## Verbatim flow.md

# User flow

1. From a goal that requires camera capture, the app shows a "Record proof" button on the goal detail screen.
2. User taps "Record proof". App requests camera and microphone permission if not already granted.
   - If the user grants permissions, continue to step 3.
   - If the user denies permissions, app shows "Camera access is required to submit this proof" with an "Open settings" link and a "Cancel" link. Cancel returns to the goal detail screen.
3. App displays the camera preview with a "Start recording" button.
4. User taps "Start recording". App begins recording. The button label becomes "Stop recording" and an elapsed-time indicator counts up.
5. User taps "Stop recording" (or the optional max-duration limit elapses). App stops recording and shows a preview of the captured video with two buttons: "Retake" and "Use this video".
   - "Retake" returns to step 3 (preview ready, no recording yet).
6. User taps "Use this video". App shows an "Uploading…" progress indicator and uploads the video to the backend.
7. On successful upload, app navigates to the goal detail screen showing "Proof uploaded — awaiting verification".
8. Failure modes:
   - Network error during upload — app shows "Upload failed — retry?" with a "Retry" button and a "Save and try later" button. Save-and-try-later persists the file locally for the next app launch.
   - Server returns 413 (file too large) — app shows "Video is too large — try a shorter recording" with a "Retake" button.
   - Server returns 415 (unsupported media type) — app shows "Unsupported video format" with a "Retake" button.

## Verbatim api_spec.md

# API spec

## Endpoints

### `POST /api/uploads/video`

- **Method:** POST
- **Path:** `/api/uploads/video`
- **Request body:** `multipart/form-data`
  - `file` — required; video file, `video/mp4` or `video/quicktime`.
  - `duration_seconds` — required; number; recorded duration in seconds.
  - `goal_id` — optional; UUID of the goal this upload is associated with. If absent, the upload is stored as orphan and may be associated later.
- **Response body (success):**
  ```json
  {
    "upload_id": "<uuid>",
    "sha256": "<hex>",
    "size_bytes": 12345678,
    "duration_seconds": 12.5,
    "mime_type": "video/mp4"
  }
  ```
- **Success status:** `201`
- **Error statuses:**
  - `401` — unauthenticated
  - `403` — `goal_id` provided but goal not owned by the authenticated user
  - `413` — file exceeds configured max size
  - `415` — unsupported media type
  - `422` — invalid form data (missing `file` or `duration_seconds`)

### `GET /api/uploads/{upload_id}`

- **Method:** GET
- **Path:** `/api/uploads/{upload_id}`
- **Request body:** `(none)`
- **Response body (success):**
  ```json
  {
    "upload_id": "<uuid>",
    "goal_id": "<uuid or null>",
    "sha256": "<hex>",
    "size_bytes": 12345678,
    "duration_seconds": 12.5,
    "mime_type": "video/mp4",
    "created_at": "2026-05-25T17:00:00Z"
  }
  ```
- **Success status:** `200`
- **Error statuses:**
  - `401` — unauthenticated
  - `403` — upload not owned by authenticated user
  - `404` — upload not found

## Context pointers

- [Source: context/project.md#Stack]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on backend HTTP behavior]

## Verbatim direction acceptance criteria

- A reusable `<CameraCapture>` component lives at `frontend/components/CameraCapture.tsx`. It:
  - Requests Expo camera and microphone permissions on mount if not already granted.
  - Renders a camera preview with a single "Start recording" button when ready.
  - Toggles to "Stop recording" + elapsed-time indicator while recording.
  - Auto-stops when an optional `maxDurationSeconds` prop is reached.
  - Shows a "Retake" / "Use this video" choice after a recording is captured.
  - Calls an `onCaptured(asset)` prop when the user confirms.
- Denied permissions surface a clear in-screen message ("Camera access is required to submit this proof") with a "Open settings" link and a "Cancel" link that returns the user to the prior screen. The component does not crash on permission denial.
- A new backend route `POST /api/uploads/video` accepts multipart uploads. See `api_spec.md`.
- A new table `media_uploads` persists per-upload metadata: `id`, `user_id`, `goal_id` (nullable), `sha256`, `size_bytes`, `duration_seconds`, `mime_type`, `storage_path`, `created_at`. Migration generated via Alembic autogenerate.
- Recorded videos are stored under a configurable path keyed by `(user_id, goal_id_or_unassigned, upload_id)`. Default: `${SACRIFICE_MEDIA_DIR:-/var/sacrifice/media}/<user_id>/<goal_or_orphan>/<upload_id>.mp4`. The setting lives in `backend/app/config.py`.
- A new endpoint `GET /api/uploads/{upload_id}` returns upload metadata for the owning user only. 403 for non-owners; 404 for unknown ids.
- `backend/app/services/uploads.py` encapsulates path resolution, write, hash computation, and metadata persistence. The route is thin; the service is unit-testable.
- An E2E `@smoke` Playwright test uploads a fixture video via the API and asserts a 201 with the expected response shape. (Pure HTTP test — does not exercise the Expo capture component.)
- A unit test verifies the `<CameraCapture>` component renders the denied-permission state when Expo's permission mock returns denied; does not crash.
- A new context module `context/modules/media.md` documents the capture component, the upload endpoint, and the storage convention.
- `context/architecture-diagrams.md` is rewritten to show the media upload path in the primary system flow.

# References

- Direction: Camera capture pipeline (Expo capture component + backend upload)
- `backend/app/routes/` upload routing surface
- `backend/app/services/uploads.py`

# Dev Agent Record

## Status
Implemented; broader backend suite has unrelated blockers

## Notes
- `POST /api/uploads/video` is present and accepts authenticated `multipart/form-data` with `file`, `duration_seconds`, and optional `goal_id`.
- The route enforces the closed error contract from the story/operator note: `401`, `403`, `413`, `415`, and `422`; a syntactically valid but nonexistent `goal_id` returns `403`, not `404`.
- The route stays thin by delegating file write, path resolution, SHA-256 computation, and metadata persistence to `backend/app/services/uploads.py`.
- To keep migration-based tests working after newer schema work introduced two Alembic heads, a no-op merge migration was added so `alembic upgrade head` resolves to a single head again.
- Verified green: `python -m pytest tests/test_uploads_api.py tests/services/test_uploads.py tests/test_media_uploads.py tests/test_chat_sessions_api.py -q` (`45 passed`).
- `python -m pytest -q` still fails in unrelated pre-existing areas (`backend/e2e_test.py`, proof-submission/notification tests, and goal-type smoke discovery), so the full backend suite is not green yet.

## File List
- `backend/app/routes/uploads.py` — authenticated thin `POST /api/uploads/video` route with MIME, ownership, and size checks before service delegation
- `backend/app/config.py` — upload-size configuration used for `413` enforcement
- `backend/app/main.py` — uploads router registration
- `backend/tests/conftest.py` — includes `media_uploads` in test-database truncation/cleanup
- `backend/tests/test_uploads_api.py` — API coverage for `201`, `401`, `403`, `413`, `415`, and `422` endpoint behavior
- `backend/alembic/versions/c4d5e6f7a8b9_merge_goal_status_and_chat_session_heads.py` — no-op merge migration restoring a single Alembic head

# Senior Developer Review

- Pending

# Review Follow-ups

- (none)
