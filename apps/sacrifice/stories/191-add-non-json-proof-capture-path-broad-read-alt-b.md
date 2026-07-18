# Story

## Title
Add non-JSON proof capture path — broad read

## Slug
`add-non-json-proof-capture-path-broad-read-alt-b`

## Scope
`backend`

## Summary
Enable the backend proof submission path to accept at least one non-JSON submission method end-to-end while preserving existing JSON proof behavior. This story is the backend contract slice that unblocks a client proof picker/upload flow.

## Acceptance Criteria
1. The proof flow offers at least one non-JSON evidence input method such as camera capture or file upload.
2. Submitted non-JSON proof is accepted end-to-end by the client and backend.

### Testable Claims (EARS)
AC1.1: WHEN a user reaches the proof flow, THE system SHALL offer at least one non-JSON evidence input method.
AC1.2: WHEN a user uses the offered non-JSON evidence input method, THE system SHALL support a method such as camera capture or file upload.
AC2.1: WHEN the client submits non-JSON proof, THE backend SHALL accept that submission.
AC2.2: WHEN a user submits non-JSON proof through the client, THE system SHALL accept the proof end-to-end across client and backend.

## Tasks / Subtasks
- [ ] Confirm current submit-proof endpoint request parsing and storage behavior in `backend/app/routes/goals.py` and `backend/app/models/proof.py`
- [ ] Define one backend-supported non-JSON proof ingestion path using multipart/form-data
- [ ] Preserve existing JSON proof submission behavior on the same proof flow
- [ ] Add request parsing/validation for uploaded proof payload plus minimal metadata only if required by current endpoint semantics
- [ ] Persist uploaded-proof representation in a way compatible with existing proof model/storage constraints
- [ ] Return existing/compatible success response shape for accepted proof submissions
- [ ] Add/adjust backend tests for JSON proof regression coverage
- [ ] Add/adjust backend tests for multipart non-JSON proof acceptance coverage
- [ ] Document any file-type/size/storage assumptions in story implementation notes if code reveals constraints not captured in direction

## Dev Notes
- Backend-first slice per PM decomposition: server-side non-JSON ingestion must land before any mobile capture/picker work.
- `flow.md` not provided in direction.
- `api_spec.md` not provided in direction.
- Broad-read interpretation for this assigned record: backend story should be scoped to a reusable non-JSON proof acceptance contract, not a UI-specific implementation detail, while still satisfying the direction's minimum of one non-JSON method.
- Existing state from context says proof submission is currently JSON-only: frontend hardcodes `Content-Type: application/json` and `JSON.stringify`, backend accepts a flat `ProofSubmissionCreate` model, and proof bodies are stored in JSONB. Backend changes here must therefore either map uploaded evidence into existing storage safely or extend storage handling without breaking current reads/writes.
- No explicit context module files were present in the prelude beyond `context/project.md` and `context/navigation.md`; do not cite missing files as authoritative sources.

### Context Pointers
- [Source: context/project.md#Identity]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on backend API or goal lifecycle]

### Direction Acceptance Criteria (verbatim)
- [ ] The proof flow offers at least one non-JSON evidence input method such as camera capture or file upload.
- [ ] Submitted non-JSON proof is accepted end-to-end by the client and backend.

## References
- `backend/app/routes/goals.py`
- `backend/app/models/proof.py`
- `backend/app/schemas/goal.py`
- `frontend/services/api.ts`
- `frontend/app.json`
- `context/project.md`
- `context/navigation.md`

## Dev Agent Record
- Status: Not started
- Implementation notes: TBD
- Test notes: TBD

## Senior Developer Review
- Review status: Pending
- Reviewer: TBD
- Review notes: TBD

## Review Follow-ups
- None yet
