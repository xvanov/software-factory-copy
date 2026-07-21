# Story

## Title
Add a factory version command printing the running git SHA and branch — broad read

## Slug
`add-a-factory-version-command-printing-the-running-git-sha-a`

## Scope
`backend`

## Summary
Add a new `factory version` CLI command backed by a pure git-inspection helper that reports the current repo short SHA, branch name, and dirty state. Keep behavior read-only and cover the helper with a temp-repo unit test that asserts reported values match actual git state.

# Acceptance Criteria

- [x] A new `factory version` CLI command prints the factory repo's current git commit SHA (short) and branch name to stdout.
- [x] It also indicates whether the working tree is dirty (has uncommitted changes).
- [x] The command is read-only (no writes, no network) and exits 0 in a valid git repo.
- [x] A unit test invokes the underlying pure helper against a temp git repo and asserts the reported SHA/branch/dirty flag match the repo state.

### Testable Claims (EARS)
AC1.1: WHEN the operator runs `factory version` in a valid factory git repo, THE CLI SHALL print the repo's current short git commit SHA to stdout.
AC1.2: WHEN the operator runs `factory version` in a valid factory git repo, THE CLI SHALL print the repo's current branch name to stdout.
AC2.1: WHEN the operator runs `factory version` in a valid factory git repo, THE CLI SHALL indicate whether the working tree is dirty.
AC3.1: WHEN `factory version` runs in a valid git repo, THE command SHALL perform no writes.
AC3.2: WHEN `factory version` runs in a valid git repo, THE command SHALL perform no network access.
AC3.3: WHEN `factory version` runs in a valid git repo, THE command SHALL exit with status 0.
AC4.1: WHEN the unit test invokes the underlying pure helper against a temp git repo, THE test SHALL assert that the reported SHA matches the repo state.
AC4.2: WHEN the unit test invokes the underlying pure helper against a temp git repo, THE test SHALL assert that the reported branch matches the repo state.
AC4.3: WHEN the unit test invokes the underlying pure helper against a temp git repo, THE test SHALL assert that the reported dirty flag matches the repo state.

# Tasks / Subtasks

- [x] Locate existing CLI command registration and output conventions.
- [x] Add `factory version` command entrypoint.
- [x] Implement pure git-inspection helper returning short SHA, branch, and dirty flag.
- [x] Ensure helper reads repo state without writes or network.
- [x] Wire command output to helper result on stdout.
- [x] Return exit code 0 in a valid git repo path.
- [x] Add unit test creating a temp git repo fixture.
- [x] In test, create committed state and assert short SHA + branch + clean state.
- [x] In test, introduce uncommitted change and assert dirty state.
- [x] Keep test isolated from ambient repo state.
- [x] Run relevant test target for the CLI/helper module.

# Dev Notes

## Direction acceptance criteria (verbatim)

- [x] A new `factory version` CLI command prints the factory repo's current git commit SHA (short) and branch name to stdout.
- [x] It also indicates whether the working tree is dirty (has uncommitted changes).
- [x] The command is read-only (no writes, no network) and exits 0 in a valid git repo.
- [x] A unit test invokes the underlying pure helper against a temp git repo and asserts the reported SHA/branch/dirty flag match the repo state.

## flow.md

(none)

## api_spec.md

(none)

## Context pointers

No canonical context files were provided in this invocation. Derive implementation entrypoints from the existing CLI codebase and preserve project conventions in-place.

## Implementation constraints

- Back the CLI command with a pure helper so the temp-repo unit test can exercise git-state detection directly.
- Report the current repo state that the running checkout reflects; do not infer from remote state.
- Use short commit SHA, branch name, and dirty-state reporting exactly as required by the direction.
- Preserve read-only behavior: no file writes, no repo mutation, no fetch/pull/network calls.
- Keep stdout output operator-oriented and deterministic enough for assertion at unit/integration boundaries.
- Broad-read interpretation: include all operator-visible dirty-state reporting needed to answer "what code is live right now?" while staying within the stated ACs.

# References

- `Direction`: `D010 factory version command for git SHA, branch, dirty`
- PM child story: `D010 add factory version CLI with git state helper and test`

# Dev Agent Record

## Implementation Log
- Added/updated pure git inspection in `factory/git_state.py` so `get_git_state(repo_root)` returns short `sha`, `branch`, and `dirty`, with read-only parsing of local `git status --porcelain` for deterministic dirty-state reporting.
- Added `factory version` command in `factory/cli.py` that reads the factory checkout state and prints SHA + branch, plus an operator-facing dirty-state suffix when uncommitted changes exist.
- Kept behavior local/read-only: only `git rev-parse` and `git status` subprocess reads; no writes and no remote/network git operations.
- Added helper-focused temp-repo coverage in `tests/test_git_state.py` and CLI smoke coverage in `tests/test_cli_version.py`, including clean and dirty repo states.

## Files Touched
- `factory/git_state.py`
- `factory/cli.py`
- `tests/test_git_state.py`
- `tests/test_cli_version.py`

## Test Evidence
- `uv run python -m pytest tests/test_git_state.py tests/test_cli_version.py -q` → pass (21 tests).
- `uv run python -m pytest -q` → pass (full suite green; 3 skipped).

# Senior Developer Review

- Pending

# Review Follow-ups

- Pending