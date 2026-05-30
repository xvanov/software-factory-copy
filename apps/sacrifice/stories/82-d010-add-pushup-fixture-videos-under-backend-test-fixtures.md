# Story
## Title
D010 add pushup fixture videos under backend test fixtures

## Scope
test

## Story
As the test harness owner,
I want canonical pushup fixture videos available under backend test fixture paths,
so the deterministic fake-factory and pushup verifier assertions can run against checked-in assets on the D008 upload path.

## Acceptance Criteria
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
- Sacrifice's chat backend records each LLM call against a new `chat_spend_ledger` table (per-user, per-call cost in millicents). Configurable cap per user per day (default $1.00). Once tripped, chat returns `429 Too Many Requests` with a clear message.
- The factory's own spend caps apply to the chain execution by construction (the factory runs the chain; the Sacrifice direction is just another input).
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
- The pushup algorithm itself (computer vision approach, frame sampling rate, landmark detection vs naive motion detection, etc.) is designed by the Architect / Dev personas during chain execution. This direction does not prescribe the CV approach. The acceptance bar is solely the fixture-based assertions above.
- Fixture videos are checked into `artifacts/` of this direction: `pushups_20.mp4`, `pushups_25.mp4`, `pushups_0.mp4` (see `artifacts/README.md` for manually-verified rep counts). The factory's chain copies them to `backend/tests/fixtures/pushup_counter/` as part of the work.
- The E2E tests for the regen and pushup validation cases use a `fake_factory_chain` fixture that:
  - Watches the directions directory for new directories created during the test.
  - Synthesizes a plausible module by reading the direction.md, invoking a deterministic generator (or a frozen LLM response cached in `tests/fixtures/llm_responses/`) and writing the module + tests to the repo.
  - Updates the direction's `state.yaml` through the lifecycle states so the Sacrifice polling endpoint observes the expected transitions.
- This keeps validation deterministic in CI. The real factory chain runs in the local dev loop, not in CI.
- `context/current-state.md`, `context/modules/backend-app.md`, and a new `context/modules/goal-type-generator.md` are rewritten to reflect the new generation pipeline. Old descriptions of typed-form goal creation are removed (not preserved as history).
- `context/architecture-diagrams.md` gains a new sequence diagram showing the chat → synthesize direction → factory chain → PR merge → notify → accept flow.

## Tasks / Subtasks
- [ ] Copy `pushups_20.mp4` into canonical backend fixture path.
- [ ] Copy `pushups_25.mp4` into canonical backend fixture path.
- [ ] Copy `pushups_0.mp4` into canonical backend fixture path.
- [ ] Preserve filenames exactly as direction artifacts specify.
- [ ] Place fixtures under `backend/tests/fixtures/pushup_counter/`.
- [ ] Add or update fixture manifest/reference used by pushup E2E/tests.
- [ ] Ensure tests consume fixtures via D008 upload path only.
- [ ] Do not add alternate upload shortcuts.
- [ ] Verify repo-relative paths are stable in CI.
- [ ] Verify large-file handling remains compatible with current test tooling.
- [ ] Document source-of-truth artifact origin in test fixture notes if a nearby README or comment exists.

## Dev Notes
[flow.md: see d010-fake-factory-chain-test-fixture-drives-direction-state-changes Dev Notes for verbatim embed]

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

Direction acceptance criteria verbatim source block:

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
- Sacrifice's chat backend records each LLM call against a new `chat_spend_ledger` table (per-user, per-call cost in millicents). Configurable cap per user per day (default $1.00). Once tripped, chat returns `429 Too Many Requests` with a clear message.
- The factory's own spend caps apply to the chain execution by construction (the factory runs the chain; the Sacrifice direction is just another input).
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
- The pushup algorithm itself (computer vision approach, frame sampling rate, landmark detection vs naive motion detection, etc.) is designed by the Architect / Dev personas during chain execution. This direction does not prescribe the CV approach. The acceptance bar is solely the fixture-based assertions above.
- Fixture videos are checked into `artifacts/` of this direction: `pushups_20.mp4`, `pushups_25.mp4`, `pushups_0.mp4` (see `artifacts/README.md` for manually-verified rep counts). The factory's chain copies them to `backend/tests/fixtures/pushup_counter/` as part of the work.
- The E2E tests for the regen and pushup validation cases use a `fake_factory_chain` fixture that:
  - Watches the directions directory for new directories created during the test.
  - Synthesizes a plausible module by reading the direction.md, invoking a deterministic generator (or a frozen LLM response cached in `tests/fixtures/llm_responses/`) and writing the module + tests to the repo.
  - Updates the direction's `state.yaml` through the lifecycle states so the Sacrifice polling endpoint observes the expected transitions.
- This keeps validation deterministic in CI. The real factory chain runs in the local dev loop, not in CI.
- `context/current-state.md`, `context/modules/backend-app.md`, and a new `context/modules/goal-type-generator.md` are rewritten to reflect the new generation pipeline. Old descriptions of typed-form goal creation are removed (not preserved as history).
- `context/architecture-diagrams.md` gains a new sequence diagram showing the chat → synthesize direction → factory chain → PR merge → notify → accept flow.

Context pointers:
- [Source: context/project.md#Stack]
- [Source: context/project.md#Top-level layout]
- [Source: context/navigation.md#When working on background verification, deadlines, or payment enforcement]
- [Source: context/navigation.md#When working on goal creation]
- [Source: context/current-state.md#<section>]
- [Source: context/modules/backend-app.md#Section]
- [Source: context/modules/backend-workers.md#Section]

Story-specific notes:
- This story is asset plumbing for later E2E coverage; keep scope to canonical fixture placement.
- Downstream pushup E2E story depends on fixture names and path stability.
- Test-Designer should validate no alternate upload path is introduced.
- If nearby context files do not actually contain the referenced sections verbatim in local checkout, load the file and use the matching feature section names present there before implementation.

## References
- `artifacts/pushups_20.mp4`
- `artifacts/pushups_25.mp4`
- `artifacts/pushups_0.mp4`
- `artifacts/README.md`
- `backend/tests/fixtures/`
- `backend/tests/fixtures/pushup_counter/`
- `backend/tests/test_youtube_*.py`
- `backend/app/goal_types/`
- `frontend/screens/GoalCreateScreen.tsx`
- `backend/app/routes/goals.py`

## Dev Agent Record
- Status: Not started
- Agent Model: TBD
- Debug Log References: TBD
- Completion Notes: TBD
- File List: TBD

## Senior Developer Review
- Status: Pending
- Reviewer: TBD
- Review Notes: TBD

## Review Follow-ups
- [ ] Pending review
