# Story

## Title
D012 restore ancestor deployed-story context via directions DB

## Slug
`d012-restore-ancestor-deployed-story-context-via-directions`

## Scope
`backend`

## Acceptance Criteria

### Verbatim Acceptance Criteria
- A `directions` table exists with one row per direction, keyed by app and
  direction id, holding at minimum: status, tracker issue number, created-at, and
  the last transition's timestamp and actor.
- `pending_directions` returns the same directions as before when the database
  is populated, reading status from the database rather than from `state.yaml`.
- A direction whose `state.yaml` is deleted keeps its status across a factory
  restart, and `pm-sync` does not re-triage it.
- Every existing on-disk direction is imported into the table by a one-time
  backfill that is safe to run twice and reports how many rows it wrote.
- `compose_context_prelude` includes a "Merged Story / Dev Agent Record" section
  for an ancestor direction that has at least one deployed story, resolved
  through the database, and omits the section when there is none.
- `state.yaml` is still written for human inspection, and a test asserts the file
  can be deleted and regenerated from the database without changing status.

### Story-Scoped Interpretation
- This story implements the `compose_context_prelude` database-backed ancestor-story lookup described in AC5.
- This story may depend on prior storage work that provides `directions` rows and `stories.direction_id` linkage, but it does not redefine those earlier slices.
- This story must preserve current behavior when `db_path` is omitted or when no deployed ancestor stories are found.

### Testable Claims (EARS)
AC1.1: WHEN evaluating this story in isolation, THE requirement is dependency context for ancestor lookup and not independently testable within this story's scope
AC2.1: WHEN evaluating this story in isolation, THE requirement targets `pending_directions` and is not independently testable within this story's scope
AC3.1: WHEN evaluating this story in isolation, THE requirement targets deletion/regeneration status persistence and is not independently testable within this story's scope
AC4.1: WHEN evaluating this story in isolation, THE requirement targets the backfill CLI and is not independently testable within this story's scope
AC5.1: WHEN `compose_context_prelude` is called with `db_path` supplied, GIVEN an ancestor direction has at least one deployed story resolved through `stories.direction_id`, THE system SHALL append a "Merged Story / Dev Agent Record" section for that ancestor direction
AC5.2: WHEN `compose_context_prelude` is called with `db_path` supplied, GIVEN an ancestor direction has no deployed story resolved through the database, THE system SHALL omit the "Merged Story / Dev Agent Record" section for that ancestor direction
AC5.3: WHEN `compose_context_prelude` is called without `db_path`, THE system SHALL append nothing for database-backed ancestor-story context
AC6.1: WHEN evaluating this story in isolation, THE requirement targets `state.yaml` regeneration behavior and is not independently testable within this story's scope

## Tasks / Subtasks
- [x] Identify current `compose_context_prelude` call sites and signature constraints
- [x] Add optional `db_path` parameter without breaking existing callers
- [x] Implement ancestor direction lookup path gated on `db_path` presence
- [x] Query deployed stories through `stories.direction_id`
- [x] Read each selected story's `story_file_path`
- [x] Reuse existing merged section formatting for "Merged Story / Dev Agent Record"
- [x] Append merged section only when at least one deployed story exists for the ancestor direction
- [x] Preserve current no-op behavior when `db_path` is absent
- [x] Preserve current no-op behavior when ancestor direction has no deployed stories
- [x] Add focused tests for positive append behavior
- [x] Add focused tests for omit behavior with no deployed stories
- [x] Add focused tests for omit behavior with no `db_path`
- [x] Confirm ancestor resolution is database-driven, not filename-derived

## Dev Notes

### Flow
(none — this is D012's ancestor-story context implementation; see the overarching D012 direction for the full operator flow)

### API Spec
(none)

### Storage contract excerpts relevant to this story
- `compose_context_prelude` gains an optional `db_path`.
- When supplied, for each ancestor direction it selects that direction's stories in state `deployed` via `stories.direction_id`, reads each one's `story_file_path`, and appends the existing "Merged Story / Dev Agent Record" section.
- With no `db_path`, or no deployed story, it appends nothing — the current behaviour.
- Nothing in a story file's name identifies its direction — only `stories.direction_id` does.

### Context pointers
- `compose_context_prelude` — `factory/context/loader.py`
- `StoryRecord`, `StoryState` — `factory/chain/state_machine.py`
- `Direction`, `MissingDirection` — `factory/directions/parser.py`
- `resolve_direction_chain` — `factory/directions/parser.py`

### Implementation guardrails
- Do not infer direction linkage from story filenames.
- Do not change behavior for callers that omit `db_path`.
- Reuse existing merged-section content shape; do not invent a new heading or payload format.
- Restrict story selection to deployed stories for the ancestor direction.
- If multiple deployed stories exist, append using the documented existing merge behavior rather than collapsing to a single arbitrary file.

## References
- Tracker: `D012 persist direction status in database`
- Direction section: `### Ancestor-story context`
- Direction section: `## Acceptance Criteria`
- Direction section: `## Storage contract`
- Flow step: `7. Confirm a persona now receives ancestor-story context.`

## Dev Agent Record
- Status: Complete
- Agent: openhands
- Branch: factory/story-129-d012-restore-ancestor-deployed-story-context-via-directions
- Notes: Implemented DB-backed ancestor-story merge in `compose_context_prelude` via optional `db_path`, reusing the existing "Merged Story / Dev Agent Record" section shape. Added ancestor lookup restricted to deployed `stories.direction_id == ancestor.id` rows, then read each `story_file_path` and append content when present. Preserved no-op behavior when `db_path` is omitted or when no deployed ancestor stories exist. Added focused tests for append/omit behavior and edge cases (multiple deployed stories, missing ancestor, empty or unreadable story file paths). Verified with `uv run pytest tests/test_context_loader.py -q` and full suite `uv run pytest -q` (green).

## File List
- `factory/context/loader.py` — Modified: added DB-backed ancestor deployed-story merge helper and optional `db_path` in `compose_context_prelude`
- `tests/test_context_loader.py` — Modified: added focused AC5.1–AC5.3 tests and DB-driven ancestor resolution coverage

## Senior Developer Review
- Status: Pending
- Reviewer: TBD
- Review notes: TBD

## Review Follow-ups
- None yet