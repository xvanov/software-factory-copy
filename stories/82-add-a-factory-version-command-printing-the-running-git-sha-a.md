# Story

## Title
Add a factory version command printing the running git SHA and branch — narrow read

## Slug
`add-a-factory-version-command-printing-the-running-git-sha-a`

## Scope
`backend`

## Summary
Add a new `factory version` CLI command backed by a pure git-state helper that reports the current short commit SHA, branch name, and dirty state for the factory repo. Keep the implementation read-only and cover the helper with a temp-repo unit test that proves reported SHA/branch/dirty values match actual repo state.

# Acceptance Criteria

- [x] A new `factory version` CLI command prints the factory repo's current git commit SHA (short) and branch name to stdout.
- [x] It also indicates whether the working tree is dirty (has uncommitted changes).
- [x] The command is read-only (no writes, no network) and exits 0 in a valid git repo.
- [x] A unit test invokes the underlying pure helper against a temp git repo and asserts the reported SHA/branch/dirty flag match the repo state.

### Testable Claims (EARS)
AC1.1: WHEN `factory version` is invoked in a valid git repo, THE CLI SHALL print the factory repo's current git commit SHA (short) to stdout.
AC1.2: WHEN `factory version` is invoked in a valid git repo, THE CLI SHALL print the factory repo's current branch name to stdout.
AC2.1: WHEN `factory version` is invoked in a valid git repo, THE CLI SHALL indicate whether the working tree is dirty.
AC3.1: WHEN `factory version` is invoked in a valid git repo, THE command SHALL perform no writes.
AC3.2: WHEN `factory version` is invoked in a valid git repo, THE command SHALL perform no network access.
AC3.3: WHEN `factory version` is invoked in a valid git repo, THE command SHALL exit with status 0.
AC4.1: WHEN the unit test invokes the underlying pure helper against a temp git repo, THE test SHALL assert that the reported SHA matches the repo state.
AC4.2: WHEN the unit test invokes the underlying pure helper against a temp git repo, THE test SHALL assert that the reported branch matches the repo state.
AC4.3: WHEN the unit test invokes the underlying pure helper against a temp git repo, THE test SHALL assert that the reported dirty flag matches the repo state.

# Tasks / Subtasks

- [x] Locate existing CLI command registration and command-handler patterns.
- [x] Add pure helper for git-state inspection scoped to factory repo path.
- [x] Return short commit SHA from helper.
- [x] Return branch name from helper.
- [x] Return dirty-state flag from helper.
- [x] Ensure helper is read-only and uses local git metadata only.
- [x] Wire new `factory version` command into CLI.
- [x] Print SHA, branch, and dirty-state to stdout.
- [x] Exit 0 in valid git repo path.
- [x] Add unit test creating temp git repo fixture.
- [x] In test, create committed state and assert helper SHA/branch values.
- [x] In test, introduce uncommitted change and assert dirty flag changes.
- [x] Keep test focused on helper, not shelling through full CLI unless already idiomatic.

# Dev Notes

## Direction inputs
[flow.md: none]
[api_spec.md: none]

## Context pointers
No canonical context files were provided in this invocation. Derive implementation points from repository code structure during development.

## Acceptance criteria (verbatim embed)
- [x] A new `factory version` CLI command prints the factory repo's current git commit SHA (short) and branch name to stdout.
- [x] It also indicates whether the working tree is dirty (has uncommitted changes).
- [x] The command is read-only (no writes, no network) and exits 0 in a valid git repo.
- [x] A unit test invokes the underlying pure helper against a temp git repo and asserts the reported SHA/branch/dirty flag match the repo state.

## Implementation boundaries
- Narrow read: implement only the operator-visible `factory version` command plus the minimal pure helper and unit test required by the direction.
- Do not expand into unrelated diagnostics, environment reporting, build metadata, remote git inspection, or docs churn unless directly required to register the command.
- Prefer an output shape that is stable and directly testable for presence of SHA, branch, and dirty indicator without adding extra claims not in the direction.
- Helper must remain pure with respect to caller-visible side effects: inspect repo state, return values, no writes.
- Unit test should validate helper behavior against a temporary local git repository under controlled committed and dirty states.

# References

- Direction: `D010 factory version command for git SHA, branch, dirty`
- PM tracker title: `D010 factory version command for git SHA, branch, dirty`
- PM rationale: one vertical slice covering helper, CLI command, and temp-repo unit test.

# Dev Agent Record

- Status: Complete
- Agent: Amelia (Dev)
- Branch: `factory/story-82-add-a-factory-version-command-printing-the-running-git-sha-a`
- Notes:
  - Added pure `get_git_state()` helper in `factory/git_state.py` returning `GitState(sha, branch, dirty)` dataclass. Uses `subprocess.run` with `git rev-parse --short HEAD`, `git rev-parse --abbrev-ref HEAD`, and `git status --porcelain`. Read-only, no network, timeout-guarded.
  - Added `factory version` CLI command in `factory/cli.py` as `@app.command("version")`. Prints `{sha} {branch}` or `{sha} {branch} (dirty)` to stdout. Exits 0.
  - Test file `tests/test_cli_version.py` has 11 tests: 6 helper-level tests against temp git repos (SHA match, branch match, dirty false/true for modified file, dirty true for untracked file, read-only idempotency) and 5 CLI-level smoke tests (exit 0, SHA in output, branch in output, dirty indicator present/absent).
  - Full test suite: passes (3 skipped, 0 failures).

## File List
- `factory/git_state.py` — new: `GitState` dataclass + `get_git_state()` helper
- `factory/cli.py` — `version_cmd` added after `apps_cmd`
- `tests/test_cli_version.py` — new: 11 tests

# Senior Developer Review

- Status: Pending
- Reviewer: TBD
- Review notes:
  - TBD

# Review Follow-ups

- None yet.