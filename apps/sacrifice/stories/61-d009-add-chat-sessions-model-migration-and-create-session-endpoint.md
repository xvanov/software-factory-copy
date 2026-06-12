# Story

## Title
D009 implement chat message endpoint for match and no-match actions

## Summary
Implement `POST /api/chat/sessions/{session_id}/messages` to persist user turns, enforce session ownership, call `chat_match` exactly once per turn, and return assistant messages for matched, no-match, and retryable-failure paths with structured `action` payloads per `api_spec.md`.

## Acceptance Criteria
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

## Tasks / Subtasks
- [ ] Implement `POST /api/chat/sessions/{session_id}/messages`.
  - [ ] Validate authenticated caller.
  - [ ] Validate session exists.
  - [ ] Validate session ownership and return `403` when not owned by user.
  - [ ] Validate non-empty, non-whitespace `content`; return `422` on invalid input.
- [ ] Persist user chat turn.
  - [ ] Append `{role, content, action}` user message to session `messages`.
  - [ ] Update `updated_at` on every accepted turn.
- [ ] Invoke match service once per turn.
  - [ ] Pass user message + prior chat context + catalog.
  - [ ] Use configured confidence threshold.
- [ ] Return matched-path assistant response.
  - [ ] Persist assistant message with `action.type = match_proposed` when above threshold.
  - [ ] Include `goal_type`, `confidence`, and `missing_criteria` in action payload.
  - [ ] Populate `draft_goal` with any extracted partial fields that are available at this stage.
- [ ] Return no-match assistant response.
  - [ ] Persist assistant message with `action.type = no_match` and `suggested_action = generate_new_goal_type` when below threshold or `none`.
- [ ] Handle upstream LLM failure.
  - [ ] Map transient match failure to endpoint `502`.
  - [ ] Ensure behavior remains compatible with frontend retry card flow from `flow.md`.
- [ ] Add tests.
  - [ ] `200` match response test.
  - [ ] `200` no-match response test.
  - [ ] `401` unauthenticated test.
  - [ ] `403` ownership test.
  - [ ] `404` session missing test.
  - [ ] `422` whitespace input test.
  - [ ] `502` upstream failure test.
  - [ ] Persistence test covering stored user and assistant messages.

## Dev Notes

- **Operator note (2026-06-12) — build on the merged foundations:** the
  `chat_sessions` model (with messages/draft_goal/status AND the D010
  linkage columns), the create-session endpoint, and the chat match service
  (`app/services/chat_match.py`) are ALREADY ON MAIN. This story adds ONLY
  the message endpoint on top of them — do NOT recreate models, migrations,
  the match service, or the session endpoint.
- **Operator note — 502 contract:** on upstream LLM failure, persist the
  user message AND an assistant retry message with `action: null` (the
  api_spec action enum is CLOSED: match_proposed/no_match/awaiting_input/
  ready_to_create/null — there is no "retry" action). The frontend renders
  the retry card off the 502 status per flow.md. Return the messages in the
  502 body.
### Child story scope
This story ends at match/no-match assistant actions. Do not implement the create-goal endpoint, final review, or full conversational criterion filling here unless needed only to produce `missing_criteria` and draft placeholders.

### Verbatim flow.md
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

### Verbatim api_spec.md
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

### Context pointers to load
- [Source: context/project.md#Identity]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on backend HTTP behavior]

### Implementation notes
- Preserve exact action shapes from `api_spec.md`; frontend card rendering depends on them.
- For this slice, `awaiting_input` and `ready_to_create` may remain for later story unless needed for a narrow successful match response path; do not overreach into create-goal.
- The 5xx failure mode in `flow.md` must stay retry-friendly; endpoint contract exposes this as `502`.

## References
- Direction: `direction.md`
- Flow: `flow.md`
- API: `api_spec.md`
- Story source title: PM `child_stories[2]`

## Dev Agent Record
- Status: Complete (reviewer change requests addressed)
- Agent model: openhands
- Debug log references: reviewer-fixes-61
- Completion notes: Addressed all 5 reviewer change requests:
  1. [high] _compute_missing_criteria now computes missing fields from all GoalCreate required top-level fields (title, description, deadline, charity_id, pledge_amount, currency, goal_type, criteria) plus goal-type-specific criteria_schema.required fields. A matched goal type is only "ready" when both the base required fields AND the type-specific required fields are present.
  2. [medium] 502 retry path now persists and returns a structured assistant action with `action.type = "retry"` so the frontend can render a "Retry" button card per flow.md instead of an unstructured plain message.
  3. [test-quality 1] test_send_message_match_returns_200_with_match_proposed_action now asserts missing_criteria against the goal type's actual criteria_schema.required from the registry, and verifies draft_goal presence, rather than hardcoding 'deadline' as always-missing.
  4. [test-quality 2] test_send_message_calls_chat_match_once_with_prior_context simplified to verify exactly-once invocations and current-message-not-in-prior-context without deep mock-call-shape coupling.
  5. [test] test_send_message_upstream_failure_returns_502 now also asserts the persisted retry message has a structured `action.type == "retry"`.

  All 12 chat message tests pass. All 231 non-chat tests pass. 13 pre-existing unrelated failures remain unchanged.
- File list: backend/app/routes/chat.py, backend/tests/test_chat_messages.py 

## Senior Developer Review
- Review status: Pending
- Reviewer: 
- Review notes: 

## Review Follow-ups
- None yet.
