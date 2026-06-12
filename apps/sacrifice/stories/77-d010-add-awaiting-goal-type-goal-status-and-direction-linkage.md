# Story
## Title
D010 iterate-generated-type writes child direction with feedback

## Acceptance Criteria
- A new endpoint `POST /api/chat/sessions/{session_id}/iterate-generated-type` files a **new** Sacrifice direction with the following shape:
  - Frontmatter carries `parent_direction: <previous-id>-<previous-slug>` (e.g. `011-pushup-counter`). This is the canonical chain linkage; it is NOT encoded in the new direction's id or slug.
  - The new direction's id is whatever the global counter allocates — it MAY be `012`, or it may be far higher if other concurrent directions landed in between. The synthesis service does not assume sequentiality.
  - The new direction's slug describes the FEEDBACK substantively (e.g. `pushup-counter-side-angle`, `pushup-counter-half-rep-credit`). The slug does not encode chain position; `iterate-N` style slugs are explicitly forbidden because they break under concurrent allocation.
  - Why prose references the previous direction by id-slug ("This iterates on 011-pushup-counter to ...").
  - Acceptance Criteria say "modify the existing `backend/app/goal_types/<name>/` module to address the following feedback: ..." with the user's feedback verbatim. The previous direction's acceptance criteria are NOT restated — once factory-side chain support lands (see below), those criteria are loaded as mandatory baseline from the parent.
  - The user's pending goal stays in `awaiting_goal_type`; the chat session is re-linked to the new direction id. On the new direction's PR merge, chat re-surfaces the updated module for another accept / iterate decision.
- Factory-side support for `parent_direction` is **out of scope** for D010. The factory's parser, context loader, persona prompts, and tracker issues today do not consume the field — the data is written forward-compatibly. The consumer side is a separate factory refactor (see `~/.claude/plans/factory-direction-chains.md`); once it lands, every previously-written iteration direction becomes chain-aware retroactively without re-writes. D010's iteration flow works without the consumer side — the Dev persona reads the existing module from disk and the prior story's Dev Agent Record — but is fragile (no enforced baseline) until the consumer side ships.

## Tasks / Subtasks
- [ ] Implement iterate-generated-type endpoint.
- [ ] Validate auth and session ownership.
- [ ] Reject empty or whitespace feedback with `422`.
- [ ] Reject iteration after acceptance with `409`.
- [ ] Resolve current pending goal and previous direction linkage.
- [ ] Synthesize follow-up direction from prior id-slug + feedback.
- [ ] Write new direction directory with `parent_direction` frontmatter.
- [ ] Re-link chat session to new direction id.
- [ ] Keep goal in `awaiting_goal_type`.
- [ ] Return `202` with new and previous direction ids.
- [ ] Add tests for non-sequential/global id behavior assumptions.

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
- [Source: context/navigation.md#When working on chat or goal-type matching]
- [Source: context/navigation.md#When working on backend HTTP behavior]

### Direction acceptance criteria note
Verbatim direction acceptance criteria embedded in this story's Acceptance Criteria section.

## References
- `backend/app/services/direction_synth.py`
- `backend/app/routes/`
- `backend/app/models/goal.py`

## Dev Agent Record
- Pending

## Senior Developer Review
- Pending

## Review Follow-ups
- Pending


## Operator resolution (2026-06-12)

Delivered on main by sibling merges — iterate-generated-type (slug normalization, parent linkage, compensating cleanup) shipped via story 69's merge and operator review. Marked deployed-by-siblings without its own PR.
