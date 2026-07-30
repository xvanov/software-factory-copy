# Story

## Title
D014 add slop kind for direct test DB bootstrap calls

## Slug
`d014-add-slop-kind-for-direct-test-db-bootstrap-calls`

## Scope
`infra`

## Summary
Add a detector rule that reports a stable new slop `kind` when Python test files directly bootstrap DB/schema infra via `SQLModel.metadata.create_all`, `sqlmodel.create_engine`, or `sqlalchemy.create_engine`.

## Acceptance Criteria
- The detector reports a finding with a stable new `kind` when a test file calls `SQLModel.metadata.create_all` or a `create_engine` variant directly.
- The finding carries a `why_slop` explanation naming `factory.observability.schema.migrate`.
- A test that obtains its database through the application's initializer produces no finding.
- The existing `# noqa: slop` escape hatch suppresses the new finding on a single test.
- `factory/observability/schema.py` tests either pass cleanly or use the escape hatch where needed.
- The `tests-meaningful` gate blocks diffs introducing the new finding through the existing path, with no new gate label.
- Regression coverage flags the story-148 bad form and does not flag the fixed form.

## Dev Agent Record
- Status: Completed
- Agent Model: OpenHands (GPT-5)
- Branch: `factory-139-d014-add-slop-kind-for-direct-test-db-bootstrap-calls`
- PR: _not opened in this run_
- Completion Notes:
  - Added a new AST detector kind `direct_db_bootstrap` in `factory/chain/slop_detector.py` for test functions that directly call:
    - `SQLModel.metadata.create_all(...)`
    - `sqlmodel.create_engine(...)`
    - `sqlalchemy.create_engine(...)`
    - bare `create_engine(...)` when imported from `sqlmodel`/`sqlalchemy`
  - Added a `why_slop` message that explicitly instructs calling `factory.observability.schema.migrate`.
  - Routed findings through the existing `scan_file`/`scan_diff` plumbing used by `tests-meaningful`; no new gate label/path introduced.
  - Reused single-test `# noqa: slop` behavior by suppressing only the new `direct_db_bootstrap` finding for that test while leaving other AST detectors active.
  - Added regression and rule-shape unit coverage in `tests/test_slop_detector.py`, including story-148 bad vs fixed forms and suppression behavior.
  - Added gate-level coverage in `tests/test_gates_evaluation.py` proving the new finding blocks via existing `tests-meaningful` label/path.
  - Verified repository test suite is green with this change (`uv run pytest -q`).
- File List:
  - `factory/chain/slop_detector.py`
  - `tests/test_slop_detector.py`
  - `tests/test_gates_evaluation.py`
  - `stories/0-d014-add-slop-kind-for-direct-test-db-bootstrap-calls.md`
