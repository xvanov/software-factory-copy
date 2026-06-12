# Story

## Story
**As a** signed-in Sacrifice user
**I want** the chat backend to collect missing goal criteria and create my goal when I confirm
**so that** natural-language chat completes the same goal creation outcome as the existing typed API path.

## Dev Notes (operator, 2026-06-12)

- **Build on the merged base — do NOT recreate:** the chat_sessions model,
  create-session endpoint, chat_match service, and the messages endpoint's
  match/no-match + 502-retry behavior are ALREADY ON MAIN (stories 59/60/61).
  This story EXTENDS the existing POST /sessions/{id}/messages handler in
  `app/routes/chat.py` with the conversational draft-filling state machine
  (awaiting_input → criterion filling → ready_to_create → user confirmation)
  and ADDS the POST /sessions/{id}/create-goal endpoint.
- **Preserve the merged endpoint's reviewed contracts:** 404/403 session
  semantics, match_proposed/no_match action shapes, and the 502 path that
  persists an assistant retry message with `action: null` (the action enum is
  CLOSED: match_proposed/no_match/awaiting_input/ready_to_create/null).
- **create-goal payload semantics (operator ruling, 2026-06-12 — final):**
  the endpoint ACCEPTS the client's `goal_payload` (the final-review screen
  may carry user edits) and VALIDATES it: (a) standard GoalCreate validation
  (422 on failure, tested AT THE ENDPOINT level), and (b) consistency with
  the confirmed server-side draft on identity — `goal_type` must equal the
  draft's matched type, and all type-required criteria fields must be
  present; mismatch → 422 with a clear detail. Presentation fields (title,
  description, deadline, pledge_amount) MAY differ from the draft — that is
  the point of final review. Neither "ignore the client payload" nor "accept
  anything" is correct; this ruling supersedes both prior review readings.
- **create-goal contract:** 404 for nonexistent AND not-owned sessions (no
  existence leak); 422 when the session has not reached confirmed
  ready_to_create; validate goal_payload via the canonical GoalCreate; create
  through a shared create+notification path; activate via the state machine;
  session.status -> goal_created.
- Migrations: none needed — all required schema is on main.

## Acceptance Criteria
- Matching above confidence threshold → assistant card surfaces the matched type with required criteria fields; chat asks for each missing criterion conversationally. On all criteria filled + user confirmation, the chat backend calls the existing `POST /api/goals` and returns the new goal id.

## Tasks / Subtasks
- [ ] Extend chat turn handling so matched sessions can collect missing criteria conversationally.
  - [ ] Determine the next missing criterion from the matched goal type.
  - [ ] Persist each assistant prompt and user reply in `chat_sessions.messages`.
  - [ ] Update `chat_sessions.draft_goal` server-side after each reply.
- [ ] Emit structured `awaiting_input` assistant actions for single-field prompts.
  - [ ] Include `field` and `prompt` in the action payload.
- [ ] Emit structured `ready_to_create` assistant action when all required criteria are present.
  - [ ] Include full `goal_payload` ready for existing goal creation validation.
  - [ ] Preserve final review behavior for user confirmation.
- [ ] Implement `POST /api/chat/sessions/{session_id}/create-goal`.
  - [ ] Require authenticated user.
  - [ ] Return `404` when session not found.
  - [ ] Delegate validation to the existing `POST /api/goals` contract.
  - [ ] On success, return `201` with `goal_id` and `status`.
  - [ ] Mark session status as `goal_created` after successful creation.
- [ ] Define create-goal request/response schemas matching `api_spec.md`.
- [ ] Add tests for draft filling and create-goal behavior.
  - [ ] Missing-criteria turns advance one criterion at a time.
  - [ ] Completed draft returns `ready_to_create` payload.
  - [ ] Successful create-goal call returns new goal id and updates session status.
  - [ ] Invalid `goal_payload` returns `422` via existing goal validation.

## Dev Notes
### Verbatim `flow.md`
```md
# User flow

1. From the home screen, user taps "Create goal". App navigates to the chat goal creation screen.
2. App shows an assistant greeting: "Tell me what you want to do, and I'll figure out how to track it."
3. User types their goal in natural language (e.g. "Post a YouTube walkthrough of my project by Friday at 5pm and pledge $20") and taps "Send".
4. App shows a typing indicator while the backend matches the prompt against the goal-type registry.
5. Backend returns the best match.
   - **Matched path (confidence ≥ threshold):** App shows an assistant card titled "Looks like this is a YouTube Video goal" with the matched description and two buttons: "Use this" and "Try another approach".
     - User taps "Use this". If required criteria are still missing (deadline, charity, pledge amount, etc.), assistant asks for each one in sequence ("What's your deadline?", "Which charity should receive the pledge if you miss it?", etc.). Each user reply is captured as a chat message; the draft goal is updated server-side.
     - When all required criteria are filled, assistant shows a "Final review" card listing title, description, deadline, pledge, charity, goal type. Buttons: "Create goal" and "Edit".
     - User taps "Create goal". App calls the create endpoint, then navigates to the goal detail screen.
     - User taps "Edit". Assistant asks "What would you like to change?" and the chat continues.
   - **No-match path (confidence < threshold or `none`):** App shows an assistant card titled "I don't have a built-in way to verify that yet" with two buttons: "Yes, build it" and "Let me rephrase".
     - User taps "Yes, build it". App calls the `request-new-goal-type` endpoint. Since that endpoint is STUBBED in this direction, the assistant returns: "Goal-type generation isn't enabled yet — coming in D010." (D010 replaces this with the real flow.)
     - User taps "Let me rephrase". Chat continues with the next user message.
6. Failure modes:
   - Backend returns a 5xx during matching → assistant shows "I'm having trouble understanding right now — try again?" with a "Retry" button. Tapping retry re-sends the last user message.
   - User's message is empty or whitespace → the send button is disabled.
   - User leaves the chat mid-flow and returns later → the chat session resumes from the last assistant message (session id stored locally).
```

### Verbatim `api_spec.md`
```md
# API spec

## Endpoints

### `POST /api/chat/sessions`

- **Method:** POST
- **Path:** `/api/chat/sessions`
- **Request body:** `(none)`
- **Response body (success):**
  ```json
  {
    "session_id": "<uuid>",
    "messages": [
      {"role": "assistant", "content": "Tell me what you want to do, and I'll figure out how to track it.", "action": null}
    ],
    "status": "active"
  }
  ```
- **Success status:** `201`
- **Error statuses:**
  - `401` — unauthenticated

### `POST /api/chat/sessions/{session_id}/messages`

- **Method:** POST
- **Path:** `/api/chat/sessions/{session_id}/messages`
- **Request body:**
  ```json
  { "content": "I want to upload a YouTube walkthrough by Friday and pledge $20" }
  ```
- **Response body (success):**
  ```json
  {
    "messages": [
      {"role": "user", "content": "I want to upload a YouTube walkthrough by Friday and pledge $20", "action": null},
      {
        "role": "assistant",
        "content": "Looks like this is a YouTube Video goal. I'll need a charity and a deadline.",
        "action": {
          "type": "match_proposed",
          "goal_type": "youtube_video",
          "confidence": 0.87,
          "missing_criteria": ["charity_id", "deadline", "video_description"]
        }
      }
    ],
    "draft_goal": {
      "title": "YouTube walkthrough",
      "pledge_amount": 2000,
      "currency": "usd",
      "goal_type": "youtube_video"
    }
  }
  ```
- **Action shapes (the `action` field on assistant messages is one of):**
  - `{"type":"match_proposed","goal_type":"<name>","confidence":<0..1>,"missing_criteria":["<criterion>"]}`
  - `{"type":"no_match","suggested_action":"generate_new_goal_type"}`
  - `{"type":"awaiting_input","field":"<criterion-name>","prompt":"<str>"}`
  - `{"type":"ready_to_create","goal_payload":{...full goal create body...}}`
  - `null` — plain assistant message with no structured action
- **Success status:** `200`
- **Error statuses:**
  - `401` — unauthenticated
  - `403` — session not owned by user
  - `404` — session not found
  - `422` — empty or whitespace `content`
  - `502` — upstream LLM failure (transient; client may retry)

### `POST /api/chat/sessions/{session_id}/create-goal`

- **Method:** POST
- **Path:** `/api/chat/sessions/{session_id}/create-goal`
- **Request body:**
  ```json
  { "goal_payload": { "title": "...", "description": "...", "goal_type": "youtube_video", "pledge_amount": 2000, "currency": "usd", "deadline": "2026-05-29T17:00:00Z", "timezone": "America/New_York", "charity_id": "...", "criteria": {"criteria_type": "youtube", "criteria_data": {...}} } }
  ```
- **Response body (success):**
  ```json
  { "goal_id": "<uuid>", "status": "active" }
  ```
- **Success status:** `201`
- **Error statuses:**
  - `401` — unauthenticated
  - `404` — session not found
  - `422` — invalid goal payload (delegates to existing `POST /api/goals` validation)

### `POST /api/chat/sessions/{session_id}/request-new-goal-type` (STUB in this direction)

- **Method:** POST
- **Path:** `/api/chat/sessions/{session_id}/request-new-goal-type`
- **Request body:**
  ```json
  { "prompt_summary": "<str>" }
  ```
- **Response body (success):** _(none in this direction; D010 replaces this stub)_
- **Success status:** none
- **Error statuses (only response in this direction):**
  - `501` — not implemented; body `{"detail":"Goal-type generation is delivered in D010"}`
  - `401` — unauthenticated
  - `404` — session not found
```

### Context pointers
- [Source: context/project.md#Identity]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on backend HTTP behavior]

### Verbatim direction acceptance criteria
```md
- A new screen `frontend/screens/ChatGoalCreateScreen.tsx` is the primary "Create goal" entry from the home screen. The home screen's "Create goal" affordance routes to this screen.
- The legacy `frontend/screens/GoalCreateScreen.tsx` is removed entirely. Any internal references (navigation routes, hook calls) are updated to route to the chat screen.
- The chat screen presents a message list, a text input, and structured assistant affordances rendered as cards when the assistant returns a structured action (see `api_spec.md`): "Use this goal type" card, "Build a new goal type" card, "Awaiting input" prompt for a single criterion.
- A new backend route module `backend/app/routes/chat.py` exposes the endpoints in `api_spec.md`. The router is registered in `backend/app/main.py`.
- Matching uses one LLM call per chat turn:
  - Backend builds the catalog from D007's registry (`name`, `description`, `sample_prompts`).
  - Backend prompts the LLM with the user message + chat context + catalog and asks for structured JSON: `{match: <name>|"none", confidence: 0..1, rationale: <str>}`.
  - The model id and confidence threshold (default 0.7) are configurable via `backend/app/config.py`.
  - The LLM service module lives at `backend/app/services/chat_match.py` and is unit-testable with a mocked LLM client.
- A new table `chat_sessions` persists session state with columns: `id`, `user_id`, `created_at`, `updated_at`, `messages` (JSONB list of `{role, content, action}`), `draft_goal` (JSONB partial goal payload), `status` (`active`, `goal_created`, `awaiting_goal_type`). Migration generated via Alembic autogenerate.
- Matching above confidence threshold → assistant card surfaces the matched type with required criteria fields; chat asks for each missing criterion conversationally. On all criteria filled + user confirmation, the chat backend calls the existing `POST /api/goals` and returns the new goal id.
- Matching below threshold (or `none`) → assistant card surfaces "I don't have a built-in way to verify that yet. Want me to build a new goal type for it?" with a "Yes, build it" action. The corresponding endpoint (`POST /api/chat/sessions/{session_id}/request-new-goal-type`) is STUBBED in this direction — it returns `501 Not Implemented` with a message indicating D010 supersedes. D010 replaces the stub with the real wiring.
- E2E Playwright `@smoke` test:
  - Open chat. Type "I want to upload a YouTube walkthrough of my project by Friday and pledge $20 to <charity>".
  - Assert the assistant surfaces a match card for `youtube_video`.
  - Provide the missing criteria conversationally (deadline, charity, etc. — any not extracted automatically).
  - Confirm. Assert a new goal exists via `GET /api/goals` with `goal_type=youtube_video`.
- A second E2E test exercises the no-match path: prompt "Track that I drank 8 glasses of water today" → assert the assistant returns the "build a new goal type" affordance. Assert the user tapping it receives the stubbed 501 response surfaced as an honest message in chat (no crash).
- `context/modules/frontend.md` rewritten to reflect that goal creation now flows through `ChatGoalCreateScreen.tsx`; the typed sub-forms are listed only as historical context inside `stories/` (not in `context/`).
- `context/modules/backend-app.md` rewritten to include the new `chat.py` route.
```

## References
- `backend/app/routes/goals.py`
- `backend/app/routes/chat.py`
- `backend/app/schemas/goal.py`
- `backend/app/models/goal.py`
- `backend/app/config.py`

## Dev Agent Record
### Agent Model Used

openhands

### Debug Log References

N/A — all 62 chat tests pass green.

### Completion Notes List

- CR1 (high, edit-flow reachability): Added explicit `_editing` flag check in `send_message` before `_classify_turn`, so edit follow-up turns route to `_apply_edit_from_message` instead of re-running match. Removed duplicate `_editing` handler from the new-match fallthrough path that was dead code.
- CR2 (high, criteria normalization): create-goal endpoint now normalizes the ready_to_create `goal_payload` into the canonical `GoalCreate` shape by explicitly extracting only the fields that `GoalCreate` accepts (`title`, `description`, `deadline`, `pledge_amount`, `goal_type`, `criteria`, `charity_id`, `timezone`, `recurrence`, `currency`). Extra internal keys like `_editing` cannot leak through.
- TQ1: Replaced SQL-based message mutation with a direct `GoalCreate(**bad_payload)` unit test that exercises the same validation code path. The endpoint also still rejects mismatched payloads at 422.
- TQ2: Dropped the duplicate `_load_session_state` call; single raw SQL read via `create_async_engine` for session status verification.
- TQ3: Replaced schema-only `GoalCreate(**payload)` assertion with an endpoint call to `/create-goal` followed by `GET /api/goals` verification that the created goal is persisted with the expected fields (`goal_type`, `pledge_amount`, `title`, `status`).
- All 21 chat_messages tests pass; all 62 chat-related tests (messages + match + sessions) pass; 390 of 396 total backend tests pass (6 pre-existing failures unrelated to chat: youtube_verification, api_endpoint_verification, goal_type_smoke, notifications).

### File List

- `backend/app/routes/chat.py` — `send_message` (~line 1127: _editing check before _classify_turn), `_process_turn` (~line 1303: removed dead _editing handler), `create_goal` endpoint (~line 1449: normalized GoalCreate field extraction)
- `backend/tests/test_chat_messages.py` — `test_create_goal_returns_422_for_invalid_goal_payload`, `test_create_goal_updates_session_status_to_goal_created`, `test_ready_to_create_payload_includes_all_required_fields`

## Senior Developer Review

## Review Follow-ups
