---
title: Goal-type generator agent — chat triggers factory, factory ships the module
type: feature
priority: p1
explore: false
created_at: 2026-05-25
---

# Goal-type generator agent — chat triggers factory, factory ships the module

## Why

D007 made goal types pluggable; D008 built the camera capture pipeline; D009 added the chat surface and the matching path with a stubbed "build a new goal type" endpoint. D010 closes the loop. When the chat detects no match (or the user explicitly asks for a new goal type), the Sacrifice backend synthesizes a direction from the user's intent, writes it into the factory's `apps/sacrifice/directions/` directory via a mounted volume, and polls the direction's `state.yaml` for progress. The factory's existing TDD chain produces the new goal-type module under D007's plugin contract; on PR merge, Sacrifice fires a notification and the user accepts the result or gives feedback to iterate. Feedback appends to a `feedback.md` in the direction directory and re-triggers the factory chain on the same direction.

This is the keystone direction. Once it lands, Sacrifice can grow new goal types from natural language without a human writing code. The acceptance criteria validate two things:

1. **Regression**: the generator can regenerate one of the existing four goal types from a prompt that describes it. With the chat matcher artificially bypassed, the chain produces a module that passes the same fixtures that the existing module passes.
2. **Novel**: the canonical pushup case (D010's reason to exist) works end-to-end. From the prompt "Do 20 pushups every morning at 7am, verify with my phone camera", the factory produces a `pushup_counter` module that uses D008's camera pipeline and passes fixture-based rep-counting assertions in CI.

## Acceptance Criteria

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

## Out of scope for this direction

- Multiple concurrent generations per user (one in-flight at a time; subsequent requests return 409).
- Cross-user goal-type sharing (each generation lands in the shared codebase by virtue of merging to main, but no per-user catalog filtering).
- Live-camera pushup demo (CI fixture-based assertions are the bar; the user-facing live demo can be performed manually after merge).
