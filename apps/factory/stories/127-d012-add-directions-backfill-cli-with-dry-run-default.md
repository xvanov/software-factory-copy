# Story

## Title
D012 add directions-backfill CLI with dry-run default

## Slug
`d012-add-directions-backfill-cli-with-dry-run-default`

## Scope
`backend`

## Summary
Implement `factory directions-backfill --app <app> [--dry-run]` as an explicit, idempotent operator command that imports on-disk directions into the authoritative `directions` table, defaults to dry-run, and reports `imported=<n> skipped=<n>`.

# Acceptance Criteria

- Every existing on-disk direction is imported into the table by a one-time backfill that is safe to run twice and reports how many rows it wrote.
- A `directions` table exists with one row per direction, keyed by app and direction id, holding at minimum: status, tracker issue number, created-at, and the last transition's timestamp and actor.
- `state.yaml` is still written for human inspection, and a test asserts the file can be deleted and regenerated from the database without changing status.

### Testable Claims (EARS)
AC1.1: WHEN `factory directions-backfill --app <app>` is run against existing on-disk directions, THE CLI SHALL import directions that have no row yet into the `directions` table.
AC1.2: WHEN `factory directions-backfill --app <app>` completes, THE CLI SHALL report how many rows it wrote.
AC1.3: WHEN the one-time backfill is run twice, THE CLI SHALL be safe to run twice.
AC2.1: WHEN a direction is imported or present in authoritative storage, THE `directions` table SHALL contain one row per direction keyed by app and direction id.
AC2.2: WHEN a direction row exists, THE `directions` table SHALL hold at minimum status, tracker issue number, created-at, and the last transition's timestamp and actor.
AC3.1: WHEN direction status is projected for human inspection, THE system SHALL still write `state.yaml`.
AC3.2: WHEN `state.yaml` is deleted and regenerated from the database, THE system SHALL preserve direction status.

# Tasks / Subtasks

- [x] Add CLI verb wiring for `factory directions-backfill --app <app> [--dry-run]`
- [x] Make dry-run the default mode
- [x] Support explicit real-write mode matching operator flow expectations
- [x] Enumerate on-disk directions for the requested app
- [x] Read current on-disk direction metadata needed for insert/projection
- [x] Query `directions` table for existing `(app, direction_id)` rows
- [x] Insert only missing rows into `directions`
- [x] Preserve idempotency on repeated runs
- [x] Count imported rows
- [x] Count skipped rows
- [x] Print `imported=<n> skipped=<n>` in dry-run and real-run paths
- [x] Ensure dry-run performs no writes
- [x] Reuse existing status parsing from on-disk state when present
- [x] Fallback missing on-disk state to `created` per direction contract
- [x] Cover command invocation parsing in tests
- [x] Cover dry-run no-write behavior in tests
- [x] Cover first real-run import counts in tests
- [x] Cover second real-run idempotent counts in tests
- [x] Cover imported row field mapping in tests
- [x] Keep implementation isolated from runtime automatic migration paths

# Dev Notes

## Scope notes
- This story is the explicit operator migration slice only.
- Runtime read-path changes in `pending_directions` are out of scope here.
- Runtime write-path changes in `mark_direction_status` are out of scope here.
- Ancestor-story context restoration in `compose_context_prelude` is out of scope here.
- Use the existing `directions` storage contract; do not redefine schema semantics in this story.

## flow.md (verbatim embed)
# Operator flow — adopting database-backed direction status

The operator-visible behaviour of this change. Each step is something a person
does and can observe the result of.

1. **Deploy the change and start a tick.** The operator runs `factory tick --app
   factory`. The tick completes normally; no direction changes status merely
   because the schema grew.

2. **Inspect what would be imported.** The operator runs `factory
   directions-backfill --app factory` (dry-run is the default) and reads a count
   of directions that have no database row yet. Nothing is written.

3. **Import the existing directions.** The operator re-runs the command with
   `--real-run` and sees `imported=<n> skipped=0`. Every direction under
   `apps/factory/directions/` now has a row whose status matches the status that
   was in its `state.yaml`.

4. **Re-run the import.** The operator runs the same command again and sees
   `imported=0 skipped=<n>`. Running it twice changes nothing, so a nervous
   operator can always check rather than guess.

5. **Confirm the database is now authoritative.** The operator deletes one
   direction's `state.yaml`, runs `factory pm-sync --app factory`, and observes
   that the direction is NOT re-triaged and does not reappear as `created` — its
   status survived the file's deletion.

6. **Confirm the file is still there for humans.** The operator looks at the same
   direction's directory and sees `state.yaml` has been rewritten from the
   database, carrying the same status.

7. **Confirm a persona now receives ancestor-story context.** The operator files
   a direction with `parent_direction` pointing at a direction that has already
   shipped, dispatches it, and reads the composed prelude in that story's run
   record: it contains a "Merged Story / Dev Agent Record" section naming the
   parent's deployed story. Before this change that section was always absent.

## api_spec.md (verbatim embed)
(none)

## Direction acceptance criteria (verbatim embed)
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

## Implementation constraints from direction
- `factory directions-backfill --app <app> [--dry-run]`
- Imports every on-disk direction that has no row yet.
- Idempotent.
- Prints `imported=<n> skipped=<n>`.
- `--dry-run` reports without writing, and is the default, matching the other destructive-ish verbs in this CLI.
- Backfill should stay explicit CLI-driven per direction preference, not a silent automatic migration.
- No HTTP surface changes.

## Context pointers
- No canonical context files were provided in this invocation.
- Use repository code search to locate CLI entrypoint, direction filesystem readers, and DB helpers before implementation.
- Load the sibling stories for dependency order from PM result:
  - schema/storage slice precedes this story
  - read/write/runtime slices remain separate and must not be bundled

## Expected handoff boundaries
- Assume `directions` table support exists or lands first; if absent, block on the schema story rather than redefining storage locally.
- Prefer shared insert/upsert helper if introduced by the schema slice.
- Tests in this story should validate CLI behavior, import selection, output format, and idempotency.
- Do not add automatic backfill on startup/tick/pm-sync.

# References

- Direction: `D012 persist direction status in database`
- Tracker: `D012 persist direction status in database`
- PM child story title: `D012 add directions-backfill CLI with dry-run default`
- Operator command contract: `factory directions-backfill --app <app> [--dry-run]`

# Dev Agent Record

- Status: Complete
- Agent: openhands (Amelia)
- Branch: factory/story-127-d012-add-directions-backfill-cli-with-dry-run-default
- Completion Notes:
  - Implemented explicit `factory directions-backfill --app <app> [--dry-run/--real-run]` wiring in `factory/cli.py`, with dry-run as the default operator path.
  - Backfill imports only missing `(app, direction_id)` rows, is safe to re-run, and reports stable `imported=<n> skipped=<n>` counts in dry-run and real-run modes.
  - Imported row mapping preserves status, tracker issue, created-at, and last-transition metadata (`updated_at`/`updated_by`) from on-disk state when present, with contract fallback to `created`.
  - Reworked `tests/test_directions_backfill.py` to remove direct test DB bootstrap calls and validate persisted rows via `factory.observability.schema.migrate`, matching the production initializer path.
  - Full suite is green after the reviewer-requested test-quality refactor.
- File List:
  - `factory/directions/backfill.py` (modified)
  - `tests/test_directions_backfill.py` (modified)
  - `apps/factory/stories/127-d012-add-directions-backfill-cli-with-dry-run-default.md` (modified)

# Senior Developer Review

- Pending

# Review Follow-ups

- None yet