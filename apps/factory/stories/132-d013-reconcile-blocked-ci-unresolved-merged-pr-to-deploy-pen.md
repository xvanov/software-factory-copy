# Story

## Title
D013 reconcile blocked_ci_unresolved merged PR to deploy_pending

## Slug
`d013-reconcile-blocked-ci-unresolved-merged-pr-to-deploy-pen`

## Scope
`backend`

## Summary
Teach `reconcile_from_github` to inspect `blocked_ci_unresolved` stories with a positive `github_pr_number` and revive only when GitHub reports the PR as merged, using the same reconciled-merge outcome as existing mergeable states.

# Acceptance Criteria

- `reconcile_from_github` considers stories in `blocked_ci_unresolved` that carry
  a positive `github_pr_number`, in addition to the current mergeable states.
- A story in `blocked_ci_unresolved` whose PR is MERGED on GitHub advances to
  `deploy_pending`, records a `merged=True` merge-action row, and enqueues the
  deploy — the same outcome as the normal reconciled-merge path.
- A story in `blocked_ci_unresolved` whose PR is still CLOSED-unmerged stays
  blocked, so reviving is driven by the real artifact and never by the mere
  passage of time.
- Reviving a story re-evaluates its dependents: a story in
  `blocked_dependency` whose blocker reached `deployed` becomes dispatchable
  again.
- The revival is logged as a `state_drift_reconciled` anomaly, like every other
  reconciliation, so it is never silent.
- A test asserts the full path: block a story on CI, merge its PR out of band,
  run reconcile, and observe `deploy_pending` plus an unblocked dependent.

### Testable Claims (EARS)
AC1.1: WHEN `reconcile_from_github` selects stories for GitHub reconciliation, THE reconciliation query/selection logic SHALL include stories in `blocked_ci_unresolved` with a positive `github_pr_number` in addition to the existing mergeable states.
AC2.1: WHEN `reconcile_from_github` observes on GitHub that a `blocked_ci_unresolved` story's PR is merged, THE story SHALL advance to `deploy_pending`.
AC2.2: WHEN `reconcile_from_github` observes on GitHub that a `blocked_ci_unresolved` story's PR is merged, THE system SHALL record a merge-action row with `merged=True`.
AC2.3: WHEN `reconcile_from_github` observes on GitHub that a `blocked_ci_unresolved` story's PR is merged, THE system SHALL enqueue the deploy.
AC2.4: WHEN `reconcile_from_github` observes on GitHub that a `blocked_ci_unresolved` story's PR is merged, THE system SHALL produce the same outcome as the normal reconciled-merge path.
AC3.1: WHEN `reconcile_from_github` observes on GitHub that a `blocked_ci_unresolved` story's PR is closed without being merged, THE story SHALL remain `blocked_ci_unresolved`.
AC3.2: UNTESTABLE-AS-WRITTEN — "reviving is driven by the real artifact and never by the mere passage of time" does not specify a directly observable mechanism beyond the closed-unmerged story staying blocked.
AC4.1: WHEN a revived story's dependents are re-evaluated, GIVEN a dependent story is in `blocked_dependency` and its blocker has reached `deployed`, THE dependent story SHALL become dispatchable again.
AC5.1: WHEN a `blocked_ci_unresolved` story is revived by reconciliation, THE system SHALL log a `state_drift_reconciled` anomaly.
AC5.2: WHEN a `blocked_ci_unresolved` story is revived by reconciliation, THE revival SHALL not be silent.
AC6.1: WHEN the full-path regression test is executed, GIVEN a story was blocked on CI and its PR was merged out of band, THE test SHALL observe the story in `deploy_pending` after reconcile runs.
AC6.2: WHEN the full-path regression test is executed, GIVEN a dependent was blocked on the revived story, THE test SHALL observe an unblocked dependent.

# Tasks / Subtasks

- [x] Update reconciliation eligibility
  - [x] Include `blocked_ci_unresolved` in the reconciliation candidate set only when `github_pr_number > 0`
  - [x] Preserve existing eligibility for current mergeable states
- [x] Implement merged revival path
  - [x] Route merged blocked stories through the same reconciled-merge outcome as existing mergeable states
  - [x] Transition story state to `deploy_pending`
  - [x] Persist merge-action row with `merged=True`
  - [x] Enqueue deploy using existing deploy path
- [x] Preserve closed-unmerged blocked behavior
  - [x] Leave `blocked_ci_unresolved` unchanged when GitHub reports closed-unmerged
  - [x] Avoid time-based or non-artifact-based revival logic
- [x] Emit anomaly logging
  - [x] Record `state_drift_reconciled` for blocked-story revival
  - [x] Reuse existing anomaly/logging shape where available
- [x] Re-evaluate dependents on successful revival/deploy progression hook
  - [x] Ensure downstream `blocked_dependency` stories are reconsidered by existing dependency-unblock logic
  - [x] Do not broaden behavior to other blocked sink states
- [x] Add/adjust backend tests for this slice
  - [x] Cover merged blocked story -> `deploy_pending`
  - [x] Cover merge-action persistence and deploy enqueue side effects
  - [x] Cover closed-unmerged blocked story remains blocked
  - [x] Cover anomaly logging for revival

# Dev Notes

## Scope notes
- Backend slice only.
- Keep the state-machine change limited to `blocked_ci_unresolved`.
- Do not implement out-of-scope behavior for `blocked_deploy_failed` or other blocked sinks.
- The PM split the full dependent-release regression into a later `test` story; backend work here should expose the behavior cleanly without overstuffing this PR.

## Implementation pointers
- Extend the reconciliation candidate selector; do not replace the existing mergeable-state path.
- Reuse the existing reconciled-merge branch for side effects where possible so `deploy_pending`, merge-action persistence, and deploy enqueue stay behaviorally aligned.
- Closed-unmerged must remain a no-op for blocked stories.
- If cheap ancestry/default-branch validation already exists in the reconciliation path, preserve it; do not invent a new requirement beyond merged-artifact gating in this story.
- Ensure the same recovery path emits `state_drift_reconciled` rather than a bespoke anomaly type.
- Confirm whether dependent unblocking already happens on transition to `deployed`; if so, wire revival into that existing path instead of duplicating dependency logic.

# References

- Direction: D013 revive blocked_ci_unresolved after merged PR
- Tracker title: `D013 revive blocked_ci_unresolved after merged PR`
- Follow-on story: `D013 regression test for blocked CI revival and dependent release`
- Out of scope:
  - Changing when the CI-fix loop gives up, or its cap.
  - Auto-reopening PRs the factory closed.
  - Any change to `blocked_deploy_failed` or the other blocked sinks.

# Dev Agent Record

## Status
Completed

## Agent Notes
- Preserved scope to `blocked_ci_unresolved` while keeping existing mergeable reconciliation behavior unchanged.
- Reused the existing reconciled-merge side-effect path (`_record_reconciled_merge_and_enqueue_deploy`) so merged blocked stories get the same `deploy_pending` transition, `merged=True` merge-action persistence, and deploy enqueue behavior as normal reconciled merges.
- Updated reconciliation eligibility so `blocked_ci_unresolved` with a positive PR number is considered while terminal states still remain excluded.
- Added dependent re-evaluation on blocked-story revival via `_revive_dependents_of_revived_blocker`, with `_deps_none_permanently_dead` guarding that dependents only revive when no pending blocker remains in a dead-end sink.
- Added regression coverage for merged and closed-unmerged `blocked_ci_unresolved` outcomes, anomaly emission, deploy side effects, and full-path dependent revival behavior.
- Verification now green: `uv run pytest tests/test_reconcile_from_github.py tests/test_conformance.py -q` and full suite `uv run pytest -q` both pass.

## File List
- `factory/chain/orchestrator.py` — `_settled()`, `_deps_none_permanently_dead()`, `_revive_dependents_of_revived_blocker()`, `reconcile_from_github()` docstring and post-revive dependent re-evaluation hook
- `tests/test_reconcile_from_github.py` — 7 new test functions (5 core + 2 edge cases) plus updates to existing test data
- `tests/test_conformance.py` — 2 new allowlist entries for the D013 transitions

# Senior Developer Review

## Review Status
Pending

## Review Checklist
- [x] Eligibility change scoped to `blocked_ci_unresolved` with positive PR number
- [x] MERGED path matches normal reconciled-merge side effects
- [x] CLOSED-unmerged blocked story remains unchanged
- [x] `state_drift_reconciled` logged
- [x] No changes to other blocked sink states
- [x] Tests cover merged and closed-unmerged outcomes

# Review Follow-ups

- None yet.