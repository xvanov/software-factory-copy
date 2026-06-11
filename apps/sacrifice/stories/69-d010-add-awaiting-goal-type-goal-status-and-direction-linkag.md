# Story 1.1: D010 add awaiting_goal_type goal status and direction linkage

Status: ready-for-dev

## Story

As a user, I want d010 add awaiting_goal_type goal status and direction linkage, so that the documented outcome holds.

## Acceptance Criteria

1. (no explicit acceptance criteria — see Dev Notes)

## Tasks / Subtasks

- [ ] Task 1 (AC: #1)
  - [ ] Subtask 1.1

## Dev Notes

- Story carries verbatim embeds of user-supplied artifacts below.
- Read context/current-state.md and context/modules/backend.md before implementing.
[Source: context/current-state.md]
[Source: context/modules/backend.md]
- **Operator note (2026-06-11) — table ownership:** the `chat_sessions` table
  and its migration belong to D009 ("add chat_sessions model, migration, and
  create-session endpoint"), which is merging ahead of this story. Do NOT
  create `chat_sessions` (or any D009-owned table) in this story's migrations;
  this story's migration scope is the awaiting-goal-type status, direction
  linkage columns, and its own additions only. If `chat_sessions` is missing
  on this branch, rebase/merge the base branch once D009's PR lands rather
  than duplicating the table.

#### Flow (verbatim from direction)

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

#### API spec (verbatim from direction)

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

#### Acceptance criteria (verbatim from direction)

1. (no explicit acceptance criteria — see Dev Notes)

### References

- [Source: context/modules/backend.md]
- [Source: context/current-state.md]

## Dev Agent Record

### Agent Model Used

openhands (dev persona)

### Completion Notes

Attempt 8 (reviewer re-revision). Addressed all 5 reviewer code change requests and 5 test-quality findings:

**CR1 (session 404):** `request_new_goal_type` uses `_get_session_or_404` — returns 404 for unknown sessions per API spec. Tests pre-create sessions via `_ensure_session` helper. Added `test_request_new_goal_type_returns_404_for_missing_session` to prove 404 behavior.

**CR2 (notification on pr_merged):** `fire_notification_on_merge` called from `generation-status` polling when state is `pr_merged` (idempotent). Also called from `accept-generated-type` as action-based backup.

**CR3 (clear direction linkage on accept):** `accept-generated-type` now clears both `goal.awaiting_direction_id` and `session.awaiting_direction_id` before commit. Test asserts `awaiting_direction_id` is null in GET response after acceptance.

**CR4 (canonical module_name in iterate paths):** `iterate-generated-type` reads `module_name` from persisted `criteria_data` (underscore form) and uses it in direction content paths instead of hyphenated `base_slug`.

**CR5 (strict goal_type validation):** `GoalCreate.validate_goal_type` restored to constrained set `{youtube_video, api_endpoint, dev_sandbox, github_repo, __generated__}`.

**TQ1-TQ5 (test fixes):** All tests now pre-create sessions before calling generation endpoints via `_ensure_session`. Dead `pass` block removed. Test asserts cleared direction linkage after accept. New 404-on-missing-session test added. All 15 tests pass green.

### File List

- `backend/app/routes/chat.py` — CR3 (clear linkage on accept), CR4 (canonical module_name in iterate paths)
- `backend/app/schemas/goal.py` — CR5 (strict goal_type validation with `__generated__`)
- `backend/tests/test_awaiting_goal_type.py` — TQ1-TQ5 fixes, new 404 test, session pre-creation
- `backend/app/models/goal.py` — unchanged (Enum already correct with `__generated__` and `awaiting_goal_type`)
- `backend/alembic/versions/f1a2b3c4d5e6_add_awaiting_goal_type_status_and_direction_.py` — unchanged
- `backend/app/services/direction_synth.py` — unchanged

## Senior Developer Review

## Review Follow-ups

### Round 2 (2026-01-20) — All 7 items addressed

**CR1 (chat_history):** Added `chat_history: list[dict] | None = None` to `RequestNewGoalTypeBody` (`backend/app/routes/chat.py:78`). The `synthesize_direction` call now passes `body.chat_history` as the second argument (line 248).

**CR2 (slug normalization):** Rewrote the iterate slug derivation to strip chain-position tokens (`iterate`, `iteration`, `iter`) and standalone numbers, and restricted tokens (`v2`–`v5`). User feedback like "iterate 2 with side angle" now produces `pushup-counter-with-side-angle` instead of a forbidden `iterate-N` shape (`backend/app/routes/chat.py:489-507`).

**CR3 (compensating transaction):** Wrapped `write_direction` and the subsequent DB commit in separate try/except blocks; any failure in either cleans up the orphaned direction directory before re-raising (`backend/app/routes/chat.py:553-590`).

**TQ1 (404 test side-effect assertions):** `test_request_new_goal_type_returns_404_for_missing_session` now verifies no goal row was created (via fresh engine/select) and no direction directory was written (via `temp_directions_path.iterdir()`) (`backend/tests/test_awaiting_goal_type.py:259-277`).

**TQ2 (normal goal guard):** `test_normal_goal_has_null_awaiting_direction_id` is retained as the sole regression guard ensuring non-generated goals return `awaiting_direction_id: null`. No other test covers this.

**Pre-existing failures:** 6 pre-existing test failures (2× YouTube verification, 2× API endpoint verification, goal type smoke metadata, notifications auto-create) are unrelated to D010 changes; confirmed present on the pre-change commit.
