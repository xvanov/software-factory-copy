# Story
## Title
D010 fake_factory_chain test fixture drives direction state changes

## Acceptance Criteria
- The E2E tests for the regen and pushup validation cases use a `fake_factory_chain` fixture that:
  - Watches the directions directory for new directories created during the test.
  - Synthesizes a plausible module by reading the direction.md, invoking a deterministic generator (or a frozen LLM response cached in `tests/fixtures/llm_responses/`) and writing the module + tests to the repo.
  - Updates the direction's `state.yaml` through the lifecycle states so the Sacrifice polling endpoint observes the expected transitions.
- This keeps validation deterministic in CI. The real factory chain runs in the local dev loop, not in CI.
- A test-only flag `SACRIFICE_FORCE_GENERATE` (or equivalent header) bypasses the chat matcher and forces every prompt into the generation path. With that flag set, the E2E test:
  - Sends the prompt: "I'll record a YouTube video and submit the link as proof. The video should be at least 5 minutes long and cover building a feature."
  - Asserts the synthesis produced a direction directory under `apps/sacrifice/directions/`.
  - After the factory chain merges the resulting PR (the test orchestrates a short-circuit run of the chain locally — see "Test orchestration" below), asserts a new module exists at `backend/app/goal_types/<some_name>/`.
  - Asserts the new module's verifier passes the existing YouTube proof test fixtures in `backend/tests/test_youtube_*.py`, with the same inputs that the existing `youtube_video` module passes.
  - The new module gets a distinct name (e.g. `youtube_video_v2`); the original `youtube_video` module is unaffected.
- E2E test sends the canonical prompt: "I want to do 20 pushups every morning at 7am and verify with my phone camera."
- After the factory chain merges, a new module exists at `backend/app/goal_types/pushup_counter/` conforming to D007's plugin base.
- The pushup module's verifier accepts a video upload (via D008's pipeline; no parallel upload path) and a `criteria_data` payload `{"count": <int>}` and returns a verified/failed verdict.
- The module passes the following fixture-based CI assertions:
  - `verify(criteria={"count":20}, upload=pushups_20.mp4)` → `verified`
  - `verify(criteria={"count":25}, upload=pushups_20.mp4)` → `failed`
  - `verify(criteria={"count":20}, upload=pushups_25.mp4)` → `verified`
  - `verify(criteria={"count":25}, upload=pushups_25.mp4)` → `verified`
  - `verify(criteria={"count":20}, upload=pushups_0.mp4)` → `failed`

## Tasks / Subtasks
- [ ] Add `fake_factory_chain` fixture watching the directions directory.
- [ ] Implement deterministic module generation from written direction inputs.
- [ ] Advance `state.yaml` through queued/in-progress/PR lifecycle states.
- [ ] Support PR URL/state fields consumed by generation-status endpoint.
- [ ] Add frozen fixture data path for deterministic LLM/module outputs.
- [ ] Add matcher-bypass test hook via env flag or equivalent header.
- [ ] Document fixture contract in test helpers/comments where needed.

## Dev Notes
### flow.md
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
- [Source: context/project.md#Identity]
- [Source: context/modules/chat.md]
- [Source: context/modules/backend-app.md]
- [Source: context/current-state.md]

### Scope notes
- Primary consumer story for flow/api verbatim embed.
- Fixture must deterministically drive downstream regen and pushup E2E stories.

## References
- `backend/tests/`
- `backend/app/routes/`
- `backend/app/config.py`
- `stories/0-d010-e2e-regen-case-for-forced-youtube-module-generation.md`
- `stories/0-d010-e2e-pushup-generation-case-and-verifier-assertions.md`

## Dev Agent Record
- Status: Not started
- Notes: 

## Senior Developer Review
- Status: Pending
- Notes: 

## Review Follow-ups
- None yet
