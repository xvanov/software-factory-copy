---
title: Chat-driven goal creation replaces typed form
type: feature
priority: p1
explore: false
created_at: 2026-05-25
---

# Chat-driven goal creation replaces typed form

## Why

Sacrifice should be AI-first: the user describes what they want in natural language and the app figures out which goal type to use. The current `GoalCreateScreen` (with its typed sub-forms for YouTube, API endpoint, dev sandbox, GitHub repo) forces the user to know which goal type they want before they can fill in anything. With D007's plugin registry shipped, the chat backend can match a free-text prompt against existing goal types via a single LLM call against the catalog (name + description + sample prompts). This direction lands the chat surface and the matching path. D010 adds the "no match → factory generates a new goal type" path on top of the stubbed endpoint defined here.

The chat REPLACES the existing creation surface. The four typed PROOF submission screens (`ProofSubmissionScreen.tsx`, `ApiEndpointSubmissionScreen.tsx`, `DevSandboxSubmissionScreen.tsx`) are unaffected — proof submission stays as it is in this batch; only goal CREATION moves to chat.

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

## Out of scope for this direction

- Generating new goal types when no match (D010).
- Replacing proof submission with chat (future direction).
- Persisting chat history across devices (single-device session id is fine).
