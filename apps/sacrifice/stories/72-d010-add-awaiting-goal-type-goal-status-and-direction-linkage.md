# Story

## Title
D010 direction writer persists synthesized directions to volume

## Story
As the backend,
I want synthesized directions written to the mounted factory volume with correct directory semantics,
so that the factory chain can discover and process new generation requests.

## Acceptance Criteria
- The `POST /api/chat/sessions/{session_id}/request-new-goal-type` endpoint (stubbed in D009) is implemented:
  - Backend writes the synthesized direction directory to a configurable path (default mounted at `/var/factory/directions/` inside the Sacrifice container; bound to `~/software-factory/apps/sacrifice/directions/` on the host).
- A new endpoint `POST /api/chat/sessions/{session_id}/iterate-generated-type` files a **new** Sacrifice direction with the following shape:
  - Frontmatter carries `parent_direction: <previous-id>-<previous-slug>` (e.g. `011-pushup-counter`). This is the canonical chain linkage; it is NOT encoded in the new direction's id or slug.
  - The new direction's id is whatever the global counter allocates — it MAY be `012`, or it may be far higher if other concurrent directions landed in between. The synthesis service does not assume sequentiality.
  - The new direction's slug describes the FEEDBACK substantively (e.g. `pushup-counter-side-angle`, `pushup-counter-half-rep-credit`). The slug does not encode chain position; `iterate-N` style slugs are explicitly forbidden because they break under concurrent allocation.
  - Why prose references the previous direction by id-slug ("This iterates on 011-pushup-counter to ...").

## Tasks / Subtasks
- Add direction writer service for configurable base path.
- Allocate global direction id from mounted directory state.
- Create directory layout expected by factory.
- Write `direction.md` and optional `flow.md` / `api_spec.md`.
- Support iteration frontmatter with `parent_direction`.
- Preserve non-sequential global id behavior.
- Add filesystem-focused tests with temp dirs.

## Dev Notes
### flow.md
[flow.md: see d010-add-awaiting-goal-type-goal-status-and-direction-linkage Dev Notes for verbatim embed]

### api_spec.md
# API spec

## Endpoints

### `POST /api/chat/sessions/{session_id}/request-new-goal-type`

Replaces the stub from D009. Synthesizes a direction from the chat context, writes it to the factory directions volume, creates a goal in `awaiting_goal_type` status.

- **Method:** POST
- **Path:** `/api/chat/sessions/{session_id}/request-new-goal-type`
- **Request body:**
  ```json
  {
    "prompt_summary": "Do 20 pushups every morning at 7am verified with my phone camera",
    "goal_payload_draft": {
      "title": "20 morning pushups",
      "description": "Do 20 pushups every morning at 7am, verified with my phone camera.",
      "pledge_amount": 1000,
      "currency": "usd",
      "deadline": "2026-05-26T11:00:00Z",
      "timezone": "America/New_York",
      "charity_id": "<stripe-connect-id>",
      "recurrence": "daily"
    }
  }
  ```
- **Response body (success):**
  ```json
  {
    "direction_id": "011-pushup-counter",
    "goal_id": "<uuid>",
    "status": "queued"
  }
  ```
- **Success status:** `202`
- **Error statuses:**
  - `401` — unauthenticated
  - `404` — session not found
  - `409` — user already has an in-flight generation; body includes the existing `direction_id`
  - `422` — `prompt_summary` too vague (synthesis LLM refuses to produce a direction)
  - `429` — daily AI budget exceeded

### `GET /api/chat/sessions/{session_id}/generation-status`

- **Method:** GET
- **Path:** `/api/chat/sessions/{session_id}/generation-status`
- **Request body:** `(none)`
- **Response body (success):**
  ```json
  {
    "direction_id": "011-pushup-counter",
    "status": "in_progress",
    "pr_url": "https://github.com/xvanov/sacrifice/pull/47",
    "summary": "Building pushup_counter goal type — Dev iterating on tests."
  }
  ```
- **Status values:** `queued`, `in_progress`, `pr_open`, `pr_merged`, `rejected`
- **Success status:** `200`
- **Error statuses:**
  - `401` — unauthenticated
  - `404` — session has no in-flight generation

### `POST /api/chat/sessions/{session_id}/accept-generated-type`

- **Method:** POST
- **Path:** `/api/chat/sessions/{session_id}/accept-generated-type`
- **Request body:** `(none)`
- **Response body (success):**
  ```json
  { "goal_id": "<uuid>", "status": "active" }
  ```
- **Success status:** `200`
- **Error statuses:**
  - `401` — unauthenticated
  - `404` — session or pending goal not found
  - `409` — generation not yet merged (`status != pr_merged`)

### `POST /api/chat/sessions/{session_id}/iterate-generated-type`

Files a NEW follow-up direction that modifies the existing module per the user's feedback. The chat session is re-linked to the new direction; the previous direction stays merged as-is.

- **Method:** POST
- **Path:** `/api/chat/sessions/{session_id}/iterate-generated-type`
- **Request body:**
  ```json
  { "feedback": "Use a side-on camera angle; count partial reps as 0.5." }
  ```
- **Response body (success):**
  ```json
  {
    "direction_id": "047-pushup-counter-side-angle",
    "previous_direction_id": "011-pushup-counter",
    "status": "queued"
  }
  ```
- **Note on `direction_id`:** The numeric prefix is allocated by the global factory counter at write time. It is NOT guaranteed to be sequential with `previous_direction_id` — concurrent directions on unrelated work may take intermediate ids. The chain linkage lives in the new direction's `parent_direction:` frontmatter field, not in its id or slug.
- **Success status:** `202`
- **Error statuses:**
  - `401` — unauthenticated
  - `404` — session or pending goal not found
  - `409` — pending goal already accepted (can't iterate after acceptance)
  - `422` — empty or whitespace `feedback`
  - `429` — daily AI budget exceeded

### Context pointers
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on chat or goal-type matching]

### Direction acceptance criteria verbatim
- The `POST /api/chat/sessions/{session_id}/request-new-goal-type` endpoint (stubbed in D009) is implemented:
  - Backend writes the synthesized direction directory to a configurable path (default mounted at `/var/factory/directions/` inside the Sacrifice container; bound to `~/software-factory/apps/sacrifice/directions/` on the host).
- A new endpoint `POST /api/chat/sessions/{session_id}/iterate-generated-type` files a **new** Sacrifice direction with the following shape:
  - Frontmatter carries `parent_direction: <previous-id>-<previous-slug>` (e.g. `011-pushup-counter`). This is the canonical chain linkage; it is NOT encoded in the new direction's id or slug.
  - The new direction's id is whatever the global counter allocates — it MAY be `012`, or it may be far higher if other concurrent directions landed in between. The synthesis service does not assume sequentiality.
  - The new direction's slug describes the FEEDBACK substantively (e.g. `pushup-counter-side-angle`, `pushup-counter-half-rep-credit`). The slug does not encode chain position; `iterate-N` style slugs are explicitly forbidden because they break under concurrent allocation.
  - Why prose references the previous direction by id-slug ("This iterates on 011-pushup-counter to ...").

## References
- `backend/app/config.py`
- `backend/app/services/`

## Dev Agent Record
- Status: Not started
- Notes: 

## Senior Developer Review
- Pending

## Review Follow-ups
- None
