# Story

## Title
Add a --json output flag to the factory apps command — narrow read

## Slug
`add-a-json-output-flag-to-the-factory-apps-command-narrow-re`

## Scope
`backend`

## Summary
Add an additive `--json` mode to `factory apps` that emits the configured app listing as JSON to stdout while preserving the existing default table output unchanged. Deliver the flag and unit coverage together as a single CLI/backend slice.

# Acceptance Criteria

- [x] `factory apps --json` prints a JSON array to stdout, one object per configured app, with at least the keys: name, repo, self_tick_enabled, deploy_enabled.
- [x] The default `factory apps` (no --json) still prints the existing human-readable table unchanged.
- [x] The JSON output is valid, parseable JSON (e.g. json.loads succeeds) and is the only thing written to stdout in --json mode (no table).
- [x] A unit test asserts that --json emits parseable JSON containing both apps and their self_tick_enabled / deploy_enabled values, and that the non-json path still renders a table.

### Testable Claims (EARS)
AC1.1: WHEN `factory apps --json` is invoked, THE command SHALL print a JSON array to stdout.
AC1.2: WHEN `factory apps --json` is invoked and configured apps exist, THE command SHALL emit one JSON object per configured app.
AC1.3: WHEN `factory apps --json` is invoked, THE command SHALL include at least the keys `name`, `repo`, `self_tick_enabled`, and `deploy_enabled` in each app object.
AC2.1: WHEN `factory apps` is invoked without `--json`, THE command SHALL print the existing human-readable table.
AC2.2: WHEN `factory apps` is invoked without `--json`, THE command SHALL leave the default table output unchanged.
AC3.1: WHEN `factory apps --json` is invoked, THE stdout output SHALL be valid, parseable JSON.
AC3.2: WHEN `factory apps --json` is invoked, THE command SHALL write only the JSON output to stdout.
AC3.3: WHEN `factory apps --json` is invoked, THE command SHALL NOT write the table to stdout.
AC4.1: WHEN unit tests are run for the apps command, THE test suite SHALL assert that `--json` emits parseable JSON.
AC4.2: WHEN unit tests are run for the apps command, THE test suite SHALL assert that the JSON output contains both apps and their `self_tick_enabled` / `deploy_enabled` values.
AC4.3: WHEN unit tests are run for the apps command, THE test suite SHALL assert that the non-json path still renders a table.

# Tasks / Subtasks

- [x] Locate the `factory apps` command entrypoint and current table-rendering path.
- [x] Locate the underlying app-listing data source used by the command.
- [x] Add a `--json` CLI flag to `factory apps`.
- [x] Reuse the existing app-listing data source for both output modes.
- [x] Implement JSON serialization for the per-app listing.
- [x] Ensure JSON mode writes only JSON to stdout.
- [x] Preserve the no-flag path and existing table rendering behavior.
- [x] Add/modify unit tests for `--json` mode output.
- [x] Add/modify unit tests for default table mode output.
- [x] Verify tests cover both configured apps and both boolean flag fields.

# Dev Notes

## Scope Constraints
- Narrow read only: implement the additive CLI flag and minimal supporting serialization from the existing app-listing data source.
- No UX/table redesign.
- No schema/config shape changes.
- No new command; extend `factory apps` only.

## flow.md
[flow.md: none]

## api_spec.md
[api_spec.md: none]

## Context Pointers
- No canonical context files were provided in this invocation (`NO CONTEXT AVAILABLE`). Dev must inspect the codebase directly to identify the command module, listing helper, and existing tests.

## Direction Acceptance Criteria (verbatim)
- [x] `factory apps --json` prints a JSON array to stdout, one object per configured app, with at least the keys: name, repo, self_tick_enabled, deploy_enabled.
- [x] The default `factory apps` (no --json) still prints the existing human-readable table unchanged.
- [x] The JSON output is valid, parseable JSON (e.g. json.loads succeeds) and is the only thing written to stdout in --json mode (no table).
- [x] A unit test asserts that --json emits parseable JSON containing both apps and their self_tick_enabled / deploy_enabled values, and that the non-json path still renders a table.

## Implementation Handoff Notes
- Prefer one source of truth for app row data; branch only at presentation/serialization.
- Keep stdout behavior explicit in tests for both modes.
- If the command currently mixes data assembly with Rich rendering, extract the smallest possible shared structure to support both table and JSON output.
- Test assertions should validate parseability plus required fields/values, not just substring presence in raw JSON.

# References

- Direction: `D008 add --json output flag to factory apps command`
- PM tracker title: `D008 add --json output flag to factory apps command`
- Story scope rationale: single vertical slice; user value exists only when flag, output, and test coverage land together.

# Dev Agent Record

## Agent Model Used
- openhands

## Debug Log References
- N/A

## Completion Notes List
- Added `--json` flag to `factory apps` command in `factory/cli.py`. When `--json` is passed, the command calls `json.dumps(rows, default=str)` and writes the result to stdout via `typer.echo`, then exits. The default (no `--json`) path is unchanged — it still renders a Rich `Table`.
- The `list_apps()` function in `factory/app_config.py` is reused as the single data source for both modes. No schema/config shape changes were needed.
- Added 6 new tests in `tests/test_cli_apps.py`:
  1. `test_cli_apps_json_emits_parseable_json_array` — AC1.1 + AC3.1
  2. `test_cli_apps_json_contains_required_keys` — AC1.2 + AC1.3
  3. `test_cli_apps_json_boolean_field_values` — AC4.1 + AC4.2
  4. `test_cli_apps_json_no_table_output` — AC3.2 + AC3.3
  5. `test_cli_apps_json_empty_apps_emits_empty_array` — edge case: empty → valid `[]`
  6. `test_cli_apps_default_no_json_still_renders_table` — AC2.1 + AC2.2 + AC4.3
- Extracted `_setup_cli_runner` helper to reduce duplication across CLI tests.
- All 12 tests in `test_cli_apps.py` pass; full test suite is green.

## File List
- `factory/cli.py` — added `--json` option to `apps_cmd`
- `tests/test_cli_apps.py` — added 6 tests + `_setup_cli_runner` helper; refactored 2 existing tests

# Senior Developer Review

- TBD

# Review Follow-ups

- TBD