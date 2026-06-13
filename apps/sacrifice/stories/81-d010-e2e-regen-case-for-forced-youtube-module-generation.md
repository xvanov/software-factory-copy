# Story

## Title
D010 E2E regen case for forced YouTube module generation

## Story
**As a** test engineer validating D010 generation plumbing
**I want** an E2E case that forces a known YouTube-style prompt down the generation path and verifies the generated verifier against existing YouTube fixtures
**so that** Sacrifice proves the chat→direction→factory pipeline can regenerate an existing verifier class under a distinct module name without mutating the original `youtube_video` implementation.

## Acceptance Criteria
- A test-only flag `SACRIFICE_FORCE_GENERATE` (or equivalent header) bypasses the chat matcher and forces every prompt into the generation path. With that flag set, the E2E test:
  - Sends the prompt: "I'll record a YouTube video and submit the link as proof. The video should be at least 5 minutes long and cover building a feature."
  - Asserts the synthesis produced a direction directory under `apps/sacrifice/directions/`.
  - After the factory chain merges the resulting PR (the test orchestrates a short-circuit run of the chain locally — see "Test orchestration" below), asserts a new module exists at `backend/app/goal_types/<some_name>/`.
  - Asserts the new module's verifier passes the existing YouTube proof test fixtures in `backend/tests/test_youtube_*.py`, with the same inputs that the existing `youtube_video` module passes.
  - The new module gets a distinct name (e.g. `youtube_video_v2`); the original `youtube_video` module is unaffected.

## Tasks / Subtasks
- [ ] Add or extend an E2E test that exercises `POST /api/chat/sessions/{session_id}/request-new-goal-type` with generation forced for the canonical YouTube prompt. (AC: 1)
  - [ ] Enable the request path to set `SACRIFICE_FORCE_GENERATE` or equivalent test-only bypass header/config in the test harness. (AC: 1)
  - [ ] Assert the response indicates generation started and returns a `direction_id`. (AC: 1)
- [ ] Assert filesystem side effects for direction creation through the mounted directions path used in tests. (AC: 1)
  - [ ] Verify a direction directory is created under `apps/sacrifice/directions/`. (AC: 1)
  - [ ] Verify the test waits for fake-factory lifecycle completion before module assertions. (AC: 1)
- [ ] Assert generated module output after fake-factory merge. (AC: 1)
  - [ ] Verify a new module exists under `backend/app/goal_types/<some_name>/`. (AC: 1)
  - [ ] Verify the generated module name is distinct from `youtube_video`. (AC: 1)
  - [ ] Verify the original `backend/app/goal_types/youtube_video/` remains unaffected. (AC: 1)
- [ ] Reuse existing YouTube proof fixtures against the generated verifier. (AC: 1)
  - [ ] Execute the same inputs covered by `backend/tests/test_youtube_*.py` against the generated verifier. (AC: 1)
  - [ ] Assert parity of pass/fail outcomes with the existing `youtube_video` module. (AC: 1)
- [ ] Keep test orchestration deterministic in CI by consuming the fake factory chain fixture rather than the real chain. (AC: 1)

## Dev Notes
### Direction acceptance criteria (verbatim)
### Plumbing

- The `POST /api/chat/sessions/{session_id}/request-new-goal-type` endpoint (stubbed in D009) is implemented:
  - Backend uses an LLM call (configurable model) to synthesize a complete direction from the chat history: `direction.md` (title, type=`feature`, why, acceptance), and where appropriate `flow.md` and `api_spec.md`. The synthesis is service-shaped, lives in `backend/app/services/direction_synth.py`, and is unit-testable with a mocked LLM client.
  - Backend writes the synthesized direction directory to a configurable path (default mounted at `/var/factory/directions/` inside the Sacrifice container; bound to `~/software-factory/apps/sacrifice/directions/` on the host).
  - Backend returns the assigned `direction_id` (e.g. `011-pushup-counter`) and the new `goal_id` to the chat client.
- `docker-compose.yml` (and prod variant if present) bind-mounts `~/software-factory/apps/sacrifice/directions/` into the Sacrifice backend container at the configured path (rw).
- A new lifecycle state `awaiting_goal_type` is added to the `Goal.status` enum. When the user confirms "Yes, build it", Sacrifice creates the goal in `awaiting_goal_type` with a new nullable `awaiting_direction_id` column on `goals`. Migration generated via Alembic autogenerate.
- The existing deadline worker is updated to skip `awaiting_goal_type` goals (they are not yet active and should not be charged).
- A new endpoint `GET /api/chat/sessions/{session_id}/generation-status` reads the direction's `state.yaml` and returns a coarse status: `queued`, `in_progress`, `pr_open`, `pr_merged`, or `rejected`, with the PR URL when available. See `api_spec.md`.
- On `pr_merged`, Sacrifice's existing notification system fires a `goal_type_ready` notification linked to the goal. The notification type is added to the `NotificationType` enum.
- A new endpoint `POST /api/chat/sessions/{session_id}/accept-generated-type` transitions the goal from `awaiting_goal_type` to `active`. Returns 409 if the generation is not yet merged.
- A new endpoint `POST /api/chat/sessions/{session_id}/iterate-generated-type` files a **new** Sacrifice direction with the following shape:
  - Frontmatter carries `parent_direction: <previous-id>-<previous-slug>` (e.g. `011-pushup-counter`). This is the canonical chain linkage; it is NOT encoded in the new direction's id or slug.
  - The new direction's id is whatever the global counter allocates — it MAY be `012`, or it may be far higher if other concurrent directions landed in between. The synthesis service does not assume sequentiality.
  - The new direction's slug describes the FEEDBACK substantively (e.g. `pushup-counter-side-angle`, `pushup-counter-half-rep-credit`). The slug does not encode chain position; `iterate-N` style slugs are explicitly forbidden because they break under concurrent allocation.
  - Why prose references the previous direction by id-slug ("This iterates on 011-pushup-counter to ...").
  - Acceptance Criteria say "modify the existing `backend/app/goal_types/<name>/` module to address the following feedback: ..." with the user's feedback verbatim. The previous direction's acceptance criteria are NOT restated — once factory-side chain support lands (see below), those criteria are loaded as mandatory baseline from the parent.
  - The user's pending goal stays in `awaiting_goal_type`; the chat session is re-linked to the new direction id. On the new direction's PR merge, chat re-surfaces the updated module for another accept / iterate decision.
- Factory-side support for `parent_direction` is **out of scope** for D010. The factory's parser, context loader, persona prompts, and tracker issues today do not consume the field — the data is written forward-compatibly. The consumer side is a separate factory refactor (see `~/.claude/plans/factory-direction-chains.md`); once it lands, every previously-written iteration direction becomes chain-aware retroactively without re-writes. D010's iteration flow works without the consumer side — the Dev persona reads the existing module from disk and the prior story's Dev Agent Record — but is fragile (no enforced baseline) until the consumer side ships.

### Spend caps

- Sacrifice's chat backend records each LLM call against a new `chat_spend_ledger` table (per-user, per-call cost in millicents). Configurable cap per user per day (default $1.00). Once tripped, chat returns `429 Too Many Requests` with a clear message.
- The factory's own spend caps apply to the chain execution by construction (the factory runs the chain; the Sacrifice direction is just another input).

### Regen validation case (must pass)

- A test-only flag `SACRIFICE_FORCE_GENERATE` (or equivalent header) bypasses the chat matcher and forces every prompt into the generation path. With that flag set, the E2E test:
  - Sends the prompt: "I'll record a YouTube video and submit the link as proof. The video should be at least 5 minutes long and cover building a feature."
  - Asserts the synthesis produced a direction directory under `apps/sacrifice/directions/`.
  - After the factory chain merges the resulting PR (the test orchestrates a short-circuit run of the chain locally — see "Test orchestration" below), asserts a new module exists at `backend/app/goal_types/<some_name>/`.
  - Asserts the new module's verifier passes the existing YouTube proof test fixtures in `backend/tests/test_youtube_*.py`, with the same inputs that the existing `youtube_video` module passes.
  - The new module gets a distinct name (e.g. `youtube_video_v2`); the original `youtube_video` module is unaffected.

### Pushup validation case (must pass)

- E2E test sends the canonical prompt: "I want to do 20 pushups every morning at 7am and verify with my phone camera."
- After the factory chain merges, a new module exists at `backend/app/goal_types/pushup_counter/` conforming to D007's plugin base.
- The pushup module's verifier accepts a video upload (via D008's pipeline; no parallel upload path) and a `criteria_data` payload `{"count": <int>}` and returns a verified/failed verdict.
- The module passes the following fixture-based CI assertions:
  - `verify(criteria={"count":20}, upload=pushups_20.mp4)` → `verified`
  - `verify(criteria={"count":25}, upload=pushups_20.mp4)` → `failed`
  - `verify(criteria={"count":20}, upload=pushups_25.mp4)` → `verified`
  - `verify(criteria={"count":25}, upload=pushups_25.mp4)` → `verified`
  - `verify(criteria={"count":20}, upload=pushups_0.mp4)` → `failed`
- The pushup algorithm itself (computer vision approach, frame sampling rate, landmark detection vs naive motion detection, etc.) is designed by the Architect / Dev personas during chain execution. This direction does not prescribe the CV approach. The acceptance bar is solely the fixture-based assertions above.
- Fixture videos are checked into `artifacts/` of this direction: `pushups_20.mp4`, `pushups_25.mp4`, `pushups_0.mp4` (see `artifacts/README.md` for manually-verified rep counts). The factory's chain copies them to `backend/tests/fixtures/pushup_counter/` as part of the work.

### Test orchestration (how validation cases run in CI without invoking the real chain)

- The E2E tests for the regen and pushup validation cases use a `fake_factory_chain` fixture that:
  - Watches the directions directory for new directories created during the test.
  - Synthesizes a plausible module by reading the direction.md, invoking a deterministic generator (or a frozen LLM response cached in `tests/fixtures/llm_responses/`) and writing the module + tests to the repo.
  - Updates the direction's `state.yaml` through the lifecycle states so the Sacrifice polling endpoint observes the expected transitions.
- This keeps validation deterministic in CI. The real factory chain runs in the local dev loop, not in CI.

### Documentation

- `context/current-state.md`, `context/modules/backend-app.md`, and a new `context/modules/goal-type-generator.md` are rewritten to reflect the new generation pipeline. Old descriptions of typed-form goal creation are removed (not preserved as history).
- `context/architecture-diagrams.md` gains a new sequence diagram showing the chat → synthesize direction → factory chain → PR merge → notify → accept flow.

### flow.md (verbatim)
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

### api_spec.md (verbatim)
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
- [Source: context/project.md#Stack]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on chat or goal-type matching]
- [Source: context/navigation.md#When working on backend HTTP behavior]
- [Source: context/modules/backend-app.md#FastAPI entrypoint, settings, and goal-facing interfaces]
- [Source: context/modules/chat.md#current chat gap and likely integration edges]
- [Source: context/modules/goal-creation.md#current creation payload shape]
- [Source: context/current-state.md#router composition and creation constraints]
- [Source: context/current-state.md#explicit note that chat is not mounted yet]

## References
- `factory/artifacts/story_template.md`
- `backend/app/routes/goals.py`
- `backend/app/main.py`
- `backend/app/services/direction_synth.py`
- `backend/app/models/goal.py`
- `backend/app/schemas/goal.py`
- `backend/tests/test_youtube_*.py`
- `backend/app/goal_types/youtube_video/`
- `backend/app/goal_types/`
- `docker-compose.yml`
- `context/project.md`
- `context/navigation.md`

## Dev Agent Record
- Status: Complete
- Notes: All 21 D010 E2E tests pass. 6 pre-existing failures in unrelated test files (test_api_endpoint_verification.py, test_goal_type_smoke.py, test_notifications.py, test_youtube_verification.py) — unchanged by this story. Addressed all reviewer findings from the second review cycle:
  - CR1/TQ1: test_generation_without_force_header now asserts matched_existing (status=matched_existing, direction_id=null) and verifies no direction directories appear on disk. Uses patched DIRECTIONS_PATH to control filesystem side effects.
  - CR2: Removed 4 legacy hand-written verifier tests that duplicated CANONICAL_YOUTUBE_CASES. The parametrized parity tests (test_original_verifier_youtube_parity, test_generated_verifier_youtube_parity) cover all cases including duration_too_short, content_match_passes, content_mismatch_fails, unavailable_transcript_fails.
  - CR3: FakeFactoryChain now supports watcher mode (start_watching/stop_watching/wait_for). All key tests (creates_module, distinct_name, unaffected_original, _generate_verifier) use watcher + wait_for pattern instead of explicit run() — matching the real factory polling behaviour required by the acceptance criteria.
  - TQ2: test_generated_module_name_distinct_from_youtube_video now has a single assertion with descriptive failure message comparing module_name to slug.
- File List:
  - `backend/tests/test_d010_e2e_forced_generation.py` — E2E test suite (21 tests)

## Senior Developer Review
- Status: Pending
- Notes: 

## Review Follow-ups
- None yet.


## Operator resolution (2026-06-13)

Delivered/obviated by sibling merges — TestYouTubeRegenE2E in the merged test_fake_factory_chain.py (story 43 / its PR) — including test_youtube_v2_verifier_equivalent_to_youtube_v1, the force-generate env-flag and header module-discovery cases, and canonical-lifecycle+acceptance — delivers this story's forced-YouTube regen E2E and verifier-equivalence scope. This branch was a stale-base re-derivation built on an in-memory _sessions store that no longer exists (main persists sessions in the chat_sessions table). Marked deployed-by-siblings without its own PR.
