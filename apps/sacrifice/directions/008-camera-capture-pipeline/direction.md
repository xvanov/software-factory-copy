---
title: Camera capture pipeline (Expo capture component + backend upload)
type: feature
priority: p1
explore: false
created_at: 2026-05-25
---

# Camera capture pipeline (Expo capture component + backend upload)

## Why

Sacrifice's first physical-world goal type (pushup-counter, the canonical novel case validated in D010) requires recording a video on the user's phone and uploading it to the backend for verification. The Expo frontend has no camera capability today and the backend has no media upload pipeline. Building this as shared infrastructure now means every future sensor-based goal type — timers, GPS tracks, photo proofs — reuses one tested capture component and one upload endpoint instead of inventing its own. Keeping it separate from D010 also prevents D010's "did the generator work" acceptance from being entangled with "did the camera plumbing work."

## Acceptance Criteria

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

## Out of scope for this direction

- Wiring the camera component into any specific goal-type's proof submission flow — that happens in D010 (pushup) or in future per-goal-type directions.
- Server-side processing of the video (transcoding, frame extraction, CV analysis). Storage and metadata only.
- Streaming uploads. Whole-file multipart upload is enough for the videos this direction needs to support.
