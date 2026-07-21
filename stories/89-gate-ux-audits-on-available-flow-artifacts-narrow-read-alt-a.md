# Story

## Story
As the backend orchestration path that launches replay-based UX auditing,
I want the run skipped or marked not-applicable when no `flow.md` artifacts are available,
so the system does not perform evidence-free UX replay audits.

## Acceptance Criteria
- [x] UX auditor run is skipped or marked not-applicable when zero flow.md files are available.

### Testable Claims (EARS)
AC1.1: WHEN replay-based UX auditing is about to run, GIVEN zero `flow.md` files are available, THE UX auditor run SHALL be skipped or marked not-applicable.

## Tasks / Subtasks
- [x] Identify the backend decision point that launches or classifies replay-based UX auditor runs.
- [x] Detect the available `flow.md` artifact count from invocation context at that decision point.
- [x] Gate replay-based UX auditing when the available `flow.md` artifact count is zero.
- [x] Preserve existing replay-based UX auditing behavior when one or more `flow.md` artifacts are available.
- [x] Record the skipped or not-applicable outcome through the existing run/result classification path.
- [x] Add automated coverage for zero-`flow.md` gating behavior.
- [x] Add automated coverage proving replay-based UX auditing is not blocked when `flow.md` is available.
- [x] Verify no payload-enrichment changes are introduced in this story.

## Dev Notes
### Scope Notes
- Narrow-read scope for this record: implement only the guardrail at the launch/classification point for replay-based UX auditing.
- Exclude payload assembly changes for `flow.md` path/content; that belongs to the separate child story: `D011 include flow.md path and contents in UX audit payload`.
- Direction acceptance criteria not assigned to this story remain out of scope for implementation here.

### flow.md
(none)

### api_spec.md
(none)

### Direction Acceptance Criteria (verbatim)
- [x] UX auditor run is skipped or marked not-applicable when zero flow.md files are available.
- [ ] Invocation payload includes at least one flow.md path and contents before replay-based auditing runs.

### Context Pointers
- No canonical context files were provided in the prelude for this run.
- Load implementation context from repository code at the UX auditor invocation path, artifact discovery path, and run-status classification path.

### Implementation Constraints
- Gate must evaluate actual available `flow.md` artifacts before replay-based auditing starts.
- Outcome wording may follow existing system terminology so long as behavior is clearly skipped or not-applicable.
- Do not weaken existing successful-path behavior for runs where one or more `flow.md` artifacts are available.
- Do not add payload-content requirements in this story.

## References
- Direction: `Gate UX audits on available flow artifacts`
- PM tracker: `D011 gate UX audits on available flow artifacts`
- Related child story: `D011 skip or mark UX replay audit N/A without flow.md`
- Follow-on child story: `D011 include flow.md path and contents in UX audit payload`

## Dev Agent Record
- Status: Implemented
- Agent: Amelia (dev)
- Branch: factory/story-89-gate-ux-audits-on-available-flow-artifacts-narrow-read-alt-a
- Notes:
  - Added flow-artifact gate in `run_scheduled_persona` (`factory/chain/scheduled_tasks.py`): when `persona == "ux_auditor"` and `_collect_flow_artifacts` returns empty, the run is recorded through `_record_and_return` as `status="rejected"` with `error="ux_auditor_no_flow_artifacts"` before `_live_run` executes.
  - Preserved existing behavior when one or more `flow.md` artifacts are available; UX auditor live runs still reach normal execution and record successful completion.
  - Added/updated coverage in `tests/test_ux_auditor_input.py`:
    - `test_run_scheduled_persona_skips_when_ux_live_run_has_no_flow_artifact`
    - `test_live_run_is_not_blocked_when_flow_md_is_available`
    - `test_dry_run_does_not_require_flow_md_artifacts`
  - Verified no payload-enrichment code was introduced in this story slice.
  - Validation run: `uv run pytest -q` (green).

## Senior Developer Review
- Status: Pending
- Reviewer: TBD
- Notes:
  - Verify guardrail sits at the authoritative launch/classification point.
  - Verify zero-`flow.md` behavior is observable and test-covered.
  - Verify no payload-enrichment scope leaked into this slice.

## Review Follow-ups
- None yet.