# Story

## Title
Add a --json output flag to the factory apps command — broad read

## Slug
`add-a-json-output-flag-to-the-factory-apps-command-broad-rea`

## Scope
`backend`

## Acceptance Criteria
- [x] `factory apps --json` prints a JSON array to stdout, one object per configured app, with at least the keys: name, repo, self_tick_enabled, deploy_enabled.
- [x] The default `factory apps` (no --json) still prints the existing human-readable table unchanged.
- [x] The JSON output is valid, parseable JSON (e.g. json.loads succeeds) and is the only thing written to stdout in --json mode (no table).
- [x] A unit test asserts that --json emits parseable JSON containing both apps and their self_tick_enabled / deploy_enabled values, and that the non-json path still renders a table.

### Testable Claims (EARS)
AC1.1: WHEN `factory apps --json` is executed, THE command SHALL print a JSON array to stdout.
AC1.2: WHEN `factory apps --json` is executed and configured apps exist, THE command SHALL emit one JSON object per configured app.
AC1.3: WHEN `factory apps --json` emits an app object, THE object SHALL include at least the keys `name`, `repo`, `self_tick_enabled`, and `deploy_enabled`.
AC2.1: WHEN `factory apps` is executed without `--json`, THE command SHALL still print the existing human-readable table unchanged.
AC3.1: WHEN `factory apps --json` is executed, THE command output SHALL be valid, parseable JSON.
AC3.2: WHEN `factory apps --json` is executed, THE command SHALL write only the JSON output to stdout.
AC3.3: WHEN `factory apps --json` is executed, THE command SHALL NOT write the table to stdout.
AC4.1: WHEN unit tests run for the apps command, THE test suite SHALL assert that `--json` emits parseable JSON containing both apps and their `self_tick_enabled` / `deploy_enabled` values.
AC4.2: WHEN unit tests run for the apps command, THE test suite SHALL assert that the non-json path still renders a table.

## Tasks / Subtasks
- [x] Identify the `factory apps` CLI entrypoint and current table-rendering path.
- [x] Add a `--json` flag to the `factory apps` command interface.
- [x] Reuse the existing app-listing data source for both output modes.
- [x] Serialize app rows to a JSON array for `--json` mode.
- [x] Ensure each JSON object includes `name`, `repo`, `self_tick_enabled`, `deploy_enabled`.
- [x] Route `--json` mode to stdout-only JSON output.
- [x] Preserve the default non-json table path unchanged.
- [x] Add or update unit tests for JSON mode.
- [x] Add or update unit tests for default table mode.
- [x] Verify JSON output parses via `json.loads` in tests.
- [x] Verify both configured apps are present in JSON-mode tests.
- [x] Verify `self_tick_enabled` / `deploy_enabled` values in JSON-mode tests.

## Dev Notes
- No canonical context files were provided in this invocation. Build from code-first inspection of the CLI command, app configuration model, and existing command tests.
- `[flow.md: not provided in direction]`
- `[api_spec.md: not provided in direction]`
- Verbatim direction acceptance criteria:
  - [x] `factory apps --json` prints a JSON array to stdout, one object per configured app, with at least the keys: name, repo, self_tick_enabled, deploy_enabled.
  - [x] The default `factory apps` (no --json) still prints the existing human-readable table unchanged.
  - [x] The JSON output is valid, parseable JSON (e.g. json.loads succeeds) and is the only thing written to stdout in --json mode (no table).
  - [x] A unit test asserts that --json emits parseable JSON containing both apps and their self_tick_enabled / deploy_enabled values, and that the non-json path still renders a table.
- Implementation boundary:
  - Keep this additive; do not change default table formatting behavior.
  - Prefer one underlying app-listing shape consumed by both renderers.
  - Do not introduce unrelated CLI/output refactors.
- Expected touch points from PM rationale:
  - `factory apps` command.
  - App list / serialization helper if needed.
  - One unit test module.
- Test-design focus:
  - Assert stdout contents in `--json` mode are JSON-only.
  - Assert parseability and per-app field presence.
  - Assert default invocation still renders the pre-existing table path.

## References
- Direction: `D008 add --json output flag to factory apps command`
- PM tracker: `D008 add --json output flag to factory apps command`
- PM child story context: `D008 add --json mode to factory apps with coverage`

## Dev Agent Record
- Status: Complete
- Agent: openhands
- Branch: factory/story-73-add-a-json-output-flag-to-the-factory-apps-command-broad-rea
- Notes:
  - Implementation was already in place from the narrow-read story (#72, merged in bf0aa2f). All ACs verified as met.
  - `factory/cli.py` — `apps_cmd` has `--json` flag (typer.Option) that calls `json.dumps(rows, default=str)` and exits; default path renders Rich Table unchanged.
  - `factory/app_config.py` — `list_apps()` is the single data source for both modes, returning dicts with keys: `name`, `repo`, `self_tick_enabled`, `deploy_enabled`.
  - `tests/test_cli_apps.py` — 12 tests: 6 for core functionality (story #61) + 6 for --json mode (story #72), covering parseability, required keys, boolean field values, stdout purity, empty-apps edge case, and default table preservation.
  - Full test suite: all green (3 skipped, unrelated).

## Senior Developer Review
- Status: Pending
- Reviewer: TBD
- Notes:
  - Verify no regression in default human-readable output.
  - Verify JSON mode writes only parseable JSON to stdout.
  - Verify tests cover both modes.

## Review Follow-ups
- None yet.