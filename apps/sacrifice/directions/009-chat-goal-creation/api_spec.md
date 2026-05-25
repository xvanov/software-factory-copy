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
