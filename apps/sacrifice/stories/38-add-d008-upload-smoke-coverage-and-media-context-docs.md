# Story
Add D008 upload smoke coverage and media context docs

## Acceptance Criteria
AC1. An E2E `@smoke` Playwright test uploads a fixture video via the API and asserts a 201 with the expected response shape. (Pure HTTP test — does not exercise the Expo capture component.)

AC2. A new context module `context/modules/media.md` documents the capture component, the upload endpoint, and the storage convention.

AC3. `context/architecture-diagrams.md` is rewritten to show the media upload path in the primary system flow.

AC4. Preserve direction boundaries in test/docs artifacts:
- Do not wire the component into a specific goal-type submission flow here.
- Do not add server-side media processing/transcoding/CV analysis.
- Do not implement streaming uploads.
- Ensure ownership enforcement on metadata reads and goal association checks on upload are represented in smoke coverage or clearly called out for follow-on coverage if smoke scope cannot include them.

## Tasks / Subtasks
- [ ] Add Playwright smoke coverage for upload API.
  - [ ] Add/locate fixture video suitable for multipart API upload.
  - [ ] Exercise `POST /api/uploads/video` as pure HTTP test.
  - [ ] Assert `201` status.
  - [ ] Assert response shape includes `upload_id`, `sha256`, `size_bytes`, `duration_seconds`, `mime_type`.
  - [ ] Tag test as `@smoke`.
- [ ] Update context docs.
  - [ ] Create `context/modules/media.md` describing capture component, upload endpoint, and storage convention.
  - [ ] Rewrite `context/architecture-diagrams.md` to show media upload path in the primary system flow.
- [ ] Keep docs aligned to direction wording and API contract.
  - [ ] Reflect ownership-scoped metadata reads.
  - [ ] Reflect orphan-vs-goal-associated storage convention.
  - [ ] Do not document unsupported capabilities as implemented.

## Dev Notes
### Direction acceptance criteria (verbatim embed)
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

### flow.md (verbatim embed)
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

### api_spec.md (verbatim embed)
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

### Context pointers
- [Source: context/project.md#Sacrifice]
- [Source: context/navigation.md#When working on overall repository shape]
- [Source: context/navigation.md#When working on the backend API]
- [Source: context/navigation.md#When working on the Expo client]
- [Source: context/current-state.md#testing]
- [Source: context/current-state.md#documentation]

## References
- `context/architecture-diagrams.md`
- `context/project.md`
- `context/navigation.md`
- `backend/app/main.py`
- `backend/app/config.py`
- `backend/app/services/`
- `frontend/components/CameraCapture.tsx`
- `factory/artifacts/story_template.md`

## Dev Agent Record
- Status: Not started
- Notes: Awaiting implementation.

## Senior Developer Review
- Pending

## Review Follow-ups
- None yet
