# Story

## Title
Add D008 upload smoke coverage and media context docs

## Summary
Add verification and documentation for the new media subsystem: a pure-HTTP Playwright `@smoke` API upload test, a new `context/modules/media.md` module, and an updated `context/architecture-diagrams.md` that shows the media upload path. This story validates and documents the reusable capture/upload contract for future goal flows.

## Scope
test

# Acceptance Criteria

- AC1: An E2E `@smoke` Playwright test uploads a fixture video via the API and asserts a 201 with the expected response shape. (Pure HTTP test — does not exercise the Expo capture component.)
- AC2: A new context module `context/modules/media.md` documents the capture component, the upload endpoint, and the storage convention.
- AC3: `context/architecture-diagrams.md` is rewritten to show the media upload path in the primary system flow.
- AC4: Do not exercise the Expo capture component in the smoke test.

# Tasks / Subtasks

- [ ] Add Playwright smoke coverage for `POST /api/uploads/video`.
  - [ ] Use fixture video upload via pure HTTP.
  - [ ] Mark test `@smoke`.
  - [ ] Assert `201` response.
  - [ ] Assert expected response shape from `api_spec.md`: `upload_id`, `sha256`, `size_bytes`, `duration_seconds`, `mime_type`.
  - [ ] Keep test independent from Expo UI/camera flows.
- [ ] Add `context/modules/media.md`.
  - [ ] Document frontend capture component contract.
  - [ ] Document backend upload endpoint contract.
  - [ ] Document storage convention rooted at `SACRIFICE_MEDIA_DIR`.
- [ ] Rewrite `context/architecture-diagrams.md` to include media upload path in the primary system flow.
- [ ] Ensure docs reflect current implemented contract from backend/frontend stories, not future pushup-specific wiring.

# Dev Notes

## Verbatim flow.md

```md
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
```

## Verbatim api_spec.md

```md
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
```

## Context pointers

- [Source: context/project.md#Sacrifice]
- [Source: context/navigation.md#When working on overall repository shape]
- [Source: context/navigation.md#When working on the backend API]
- [Source: context/navigation.md#When working on the Expo client]

## Verbatim direction acceptance criteria

```md
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
```

## Scope notes

- This story owns smoke verification and context documentation only.
- Do not expand scope into Expo component implementation or backend route/service implementation beyond what is needed to document and validate the contract.
- Respect canonical doc locations: `context/modules/media.md` and `context/architecture-diagrams.md` only.

# References

- `context/project.md`
- `context/navigation.md`
- `context/architecture-diagrams.md`
- `context/modules/`
- `backend/app/routes/`
- `backend/app/services/`
- `frontend/components/CameraCapture.tsx`

# Dev Agent Record

## Agent Model Used

## Debug Log References

## Completion Notes List

## File List

# Senior Developer Review

## Reviewer

## Review Date

## Outcome

## Notes

# Review Follow-ups

- [ ] None recorded yet.
