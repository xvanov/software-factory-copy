# Story

## Title
D009 hook chat UI to create goal and handle stubbed 501 honestly

## Story
**As a** Sacrifice user
**I want** the chat UI to complete goal creation and surface the D010 stub honestly
**so that** matched flows end on a real goal detail screen and no-match flows fail transparently without crashing.

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
- [ ] Wire `Use this` and subsequent conversational input flow to backend message/create endpoints.
- [ ] Submit `Create goal` using `POST /api/chat/sessions/{session_id}/create-goal`.
- [ ] On successful create response, navigate to the goal detail screen for the returned `goal_id`.
- [ ] Support `Edit` continuation by returning to chat input flow.
- [ ] Wire `Yes, build it` to `POST /api/chat/sessions/{session_id}/request-new-goal-type`.
- [ ] Surface the stubbed `501` response as an honest assistant message in chat with no crash.
- [ ] Preserve `Let me rephrase` as a continuation path.
- [ ] Add frontend tests for successful create-goal navigation and truthful `501` surfaced message.
- [ ] Ensure retry interaction remains functional for transient failure path if already implemented.

## Dev Notes
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

### Context pointers
- [Source: context/project.md#Active constraints]
- [Source: context/modules/frontend.md#App shell and screen switching]
- [Source: context/modules/goal-creation.md#Current creation flow]
- [Source: context/navigation.md#When working on the Expo client]
- [Source: context/navigation.md#When working on proof submission notifications or dashboard behavior]

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
- `frontend/screens/ChatGoalCreateScreen.tsx`
- `frontend/services/api.ts`
- `frontend/hooks/useNavigation.tsx`
- `frontend/App.tsx`
- `frontend/AGENTS.md`

## Dev Agent Record
- Agent Model Used: 
- Debug Log References: 
- Completion Notes: 
- File List: 

## Senior Developer Review
- [ ] Create-goal action calls correct endpoint
- [ ] Success path navigates to goal detail by returned id
- [ ] `501` stub is surfaced honestly in chat with no crash
- [ ] `Edit` returns control to chat continuation
- [ ] Scope excludes inventing D010 UX beyond truthful message

## Review Follow-ups
- None.
