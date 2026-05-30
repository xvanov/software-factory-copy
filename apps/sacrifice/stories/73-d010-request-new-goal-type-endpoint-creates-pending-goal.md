# Story

## Title
D010 request-new-goal-type endpoint creates pending goal

## Story
As a chat generation API owner,
I want the request endpoint to synthesize a direction, write it, and create a pending goal,
so that the user can commit intent now and wait asynchronously for a generated goal type.

## Acceptance Criteria
- The `POST /api/chat/sessions/{session_id}/request-new-goal-type` endpoint (stubbed in D009) is implemented:
  - Backend uses an LLM call (configurable model) to synthesize a complete direction from the chat history: `direction.md` (title, type=`feature`, why, acceptance), and where appropriate `flow.md` and `api_spec.md`. The synthesis is service-shaped, lives in `backend/app/services/direction_synth.py`, and is unit-testable with a mocked LLM client.
  - Backend writes the synthesized direction directory to a configurable path (default mounted at `/var/factory/directions/` inside the Sacrifice container; bound to `~/software-factory/apps/sacrifice/directions/` on the host).
  - Backend returns the assigned `direction_id` (e.g. `011-pushup-counter`) and the new `goal_id` to the chat client.
- A new lifecycle state `awaiting_goal_type` is added to the `Goal.status` enum. When the user confirms "Yes, build it", Sacrifice creates the goal in `awaiting_goal_type` with a new nullable `awaiting_direction_id` column on `goals`. Migration generated via Alembic autogenerate.

## Tasks / Subtasks
- [ ] Implement `POST /api/chat/sessions/{session_id}/request-new-goal-type` in the appropriate router replacing the D009 stub.
- [ ] Validate session ownership/existence and map unauthenticated / missing session responses per spec.
- [ ] Invoke the direction synthesis service and direction writer service in sequence.
- [ ] Create the goal in `awaiting_goal_type` with `awaiting_direction_id` populated from the assigned direction id.
- [ ] Re-link chat/session state as needed so later status/accept/iterate flows can find the pending generation.
- [ ] Return `202` with `{ direction_id, goal_id, status: "queued" }`.
- [ ] Add endpoint tests covering success, `404`, synthesis-driven `422`, and in-flight conflict behavior if preconditions exist in current chat/session model.

## Dev Notes
### Verbatim flow.md
```md
# User flow

1. From within the chat goal creation screen (D009), user describes a goal whose verification doesn't match any existing type. Canonical example: "I want to do 20 pushups every morning at 7am, verify with my phone camera."
2. App shows the no-match assistant card: "I don't have a built-in way to verify that yet. Want me to build a new goal type for it? Takes a few minutes." with buttons "Yes, build it" and "Let me rephrase".
3. User taps "Yes, build it".
4. App shows: "Got it. I'm building a 'pushup-counter' goal type. I'll notify you when it's ready — feel free to close the app." A status banner appears with "Building goal type — queued".
5. Behind the scenes, the goal is created in `awaiting_goal_type` status with a reference to the direction id. A new direction directory is written under `apps/sacrifice/directions/`.
6. The status banner updates as the factory chain progresses: "queued" → "in progress" → "pull request open" → "merging".
7. Optional: user closes the app and goes about their day.
8. When the factory PR merges, the app's notification system fires a `goal_type_ready` notification. The notification bell increments.
9. User taps the notification. App opens the chat session.
10. Assistant shows: "Your 'pushup-counter' goal type is ready. It records a video of your pushups and counts reps. Want to accept and activate your goal?" with buttons "Accept and activate" and "Give feedback to iterate".
11. **Accept path:** User taps "Accept and activate". App calls the accept endpoint; goal transitions from `awaiting_goal_type` to `active`. App navigates to the goal detail screen.
12. **Iterate path:** User taps "Give feedback to iterate". App shows a text input prompt: "What should be different?". User types feedback (e.g. "Use a side-on camera angle instead of front-on; count partial reps as 0.5"). User taps "Send".
13. App shows: "Got it — I'm filing a follow-up direction that will update the module." The status banner returns to "Building goal type — queued" showing the new direction id (e.g. `047-pushup-counter-side-angle` — the id is whatever the global counter allocates; concurrent directions may interleave). The factory chain runs on the new direction; on its merge, steps 8–11 repeat against the updated module. The user's goal stays in `awaiting_goal_type` across iterations until they tap "Accept and activate".
14. Failure modes:
    - The factory chain bounces with a `(tests-need-clarification)` direction (per the chain's escalation rule). Sacrifice surfaces this in chat as: "I'm stuck — can you describe what 'counts as a pushup' more precisely?" with a free-text input that appends to the direction's `feedback.md`. The user's reply re-enters the iteration loop.
    - The user's daily AI spend cap is exhausted. Chat returns: "You've hit today's AI budget. Try again tomorrow, or reach out if this is wrong." No direction is filed.
    - The user already has an in-flight generation. Chat returns: "You're already building 'pushup-counter'. Want to add to that one instead?" — tapping yes routes feedback into the existing direction's `feedback.md`.
    - Direction synthesis fails (LLM cannot produce a coherent direction). Chat returns: "I couldn't pin down what you want — try rephrasing with more concrete success criteria."
```

### Verbatim api_spec.md
```md
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
```

### Context pointers
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on backend HTTP behavior]
- [Source: context/navigation.md#When working on chat or goal-type matching]
- [Source: context/navigation.md#When working on goal creation]

### Verbatim direction acceptance criteria
```md
## Acceptance Criteria

### Plumbing

- The `POST /api/chat/sessions/{session_id}/request-new-goal-type` endpoint (stubbed in D009) is implemented:
  - Backend uses an LLM call (configurable model) to synthesize a complete direction from the chat history: `direction.md` (title, type=`feature`, why, acceptance), and where appropriate `flow.md` and `api_spec.md`. The synthesis is service-shaped, lives in `backend/app/services/direction_synth.py`, and is unit-testable with a mocked LLM client.
  - Backend writes the synthesized direction directory to a configurable path (default mounted at `/var/factory/directions/` inside the Sacrifice container; bound to `~/software-factory/apps/sacrifice/directions/` on the host).
  - Backend returns the assigned `direction_id` (e.g. `011-pushup-counter`) and the new `goal_id` to the chat client.
- A new lifecycle state `awaiting_goal_type` is added to the `Goal.status` enum. When the user confirms "Yes, build it", Sacrifice creates the goal in `awaiting_goal_type` with a new nullable `awaiting_direction_id` column on `goals`. Migration generated via Alembic autogenerate.
```

## References
- `backend/app/routes/`
- `backend/app/models/goal.py`
- `backend/app/services/direction_synth.py`
- `backend/app/config.py`

## Dev Agent Record
- Status: Not started
- Agent Model: 
- Debug Log References: 
- Completion Notes: 
- File List: 

## Senior Developer Review
- Status: Pending
- Reviewer: 
- Review Notes: 

## Review Follow-ups
- None.
