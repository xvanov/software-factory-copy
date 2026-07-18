# Story
**Title:** Add non-JSON proof capture path — narrow read
**Slug:** add-non-json-proof-capture-path-narrow-read-alt-a
**Scope:** backend

## Acceptance Criteria
- [ ] The proof flow offers at least one non-JSON evidence input method such as camera capture or file upload.
- [ ] Submitted non-JSON proof is accepted end-to-end by the client and backend.

### Testable Claims (EARS)
AC1.1: WHEN a user reaches the proof submission flow, THE system SHALL offer at least one non-JSON evidence input method.
AC1.2: WHEN the proof submission flow offers a non-JSON evidence input method, THE system SHALL support a method such as camera capture or file upload.
AC2.1: WHEN a user submits non-JSON proof through the supported proof flow, THE client SHALL accept and send that proof to the backend.
AC2.2: WHEN the backend receives submitted non-JSON proof through the supported proof flow, THE backend SHALL accept it end-to-end.

## Tasks / Subtasks
- [ ] Confirm current submit-proof endpoint contract and JSON-only behavior in backend route/schema/model surface.
- [ ] Add one backend non-JSON ingestion path on submit-proof endpoint.
- [ ] Preserve existing JSON proof submission behavior.
- [ ] Define minimal multipart form contract for uploaded proof and any required metadata.
- [ ] Validate uploaded proof request shape and error handling.
- [ ] Persist non-JSON proof submission in a form compatible with existing proof storage/review flow.
- [ ] Return response payloads consistent with existing submit-proof endpoint expectations.
- [ ] Add/update backend tests for multipart success path.
- [ ] Add/update backend tests for JSON backward-compatibility path.
- [ ] Add/update backend tests for invalid multipart requests.
- [ ] Note any frontend contract dependency in Dev Agent Record.

## Dev Notes
### Scope Notes
- Narrow-read scope: backend-only story that enables one server-side non-JSON proof acceptance path.
- User-facing acquisition UI is out of scope here; this story must make the submit-proof endpoint capable of accepting non-JSON proof so the follow-on frontend story has a live backend contract.
- Keep the slice minimal: one working multipart/file-upload path is sufficient.
- Preserve existing JSON proof behavior.

### flow.md
No `flow.md` provided in the direction.

### api_spec.md
No `api_spec.md` provided in the direction.

### Context Pointers
- [Source: context/project.md#Identity]
- [Source: context/project.md#Stack]
- [Source: context/project.md#Active constraints]

### Direction Acceptance Criteria (verbatim)
- [ ] The proof flow offers at least one non-JSON evidence input method such as camera capture or file upload.
- [ ] Submitted non-JSON proof is accepted end-to-end by the client and backend.

### Implementation Constraints
- Current state explicitly says proof submission is JSON-only: frontend hardcodes `Content-Type: application/json` and `JSON.stringify`, while backend accepts a flat `ProofSubmissionCreate` model and stores proof bodies in JSONB.
- Direction requires at least one non-JSON input method accepted end-to-end; for this backend slice, implement acceptance for one non-JSON transport the client can call next.
- PM decomposition guidance: start with backend multipart/file acceptance so the system can actually receive non-JSON proof.
- Because no `api_spec.md` exists, this story must make the backend contract explicit in implementation and test coverage, without inventing extra product requirements beyond one multipart/file-upload path.
- Existing proof storage is JSONB-backed; if file metadata is stored in JSON, document the representation in Dev Agent Record so downstream docs can be updated accurately.
- Avoid requiring Celery unless genuinely needed.

## References
- `backend/app/routes/goals.py`
- `backend/app/models/proof.py`
- `backend/app/schemas/goal.py`
- `backend/app/main.py`
- `frontend/services/api.ts`
- `frontend/app.json`
- `PRD.md`
- `PROMPT.md`

## Dev Agent Record
- Agent Model Used: 
- Debug Log References: 
- Completion Notes: 
- File List: 

## Senior Developer Review
- Review Status: Pending
- Reviewer: 
- Review Notes: 

## Review Follow-ups
- [ ] None yet.
