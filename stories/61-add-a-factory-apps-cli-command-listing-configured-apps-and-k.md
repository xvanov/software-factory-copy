# Story

## Title
Add a factory apps CLI command listing configured apps and key config — narrow read

## Slug
`add-a-factory-apps-cli-command-listing-configured-apps-and-k`

## Scope
backend

## Summary
Implement a small, read-only `factory apps` CLI command that discovers `apps/*/config.yaml`, reads effective values needed for operator visibility, prints one row per configured app, and adds unit coverage using a temporary `apps/` tree.

# Acceptance Criteria

- [x] A new `factory apps` CLI command lists every app under apps/ that has a config.yaml, one row per app.
- [x] Each row shows at least: app name, repo, self_tick_enabled, and deploy.enabled (reading the effective values from the app's config.yaml).
- [x] The command is read-only — it never mutates any config or state — and exits 0 when at least one app is found.
- [x] A unit test invokes the command (or its underlying pure list function) against a temp apps/ tree with two app configs and asserts both apps and their self_tick_enabled / deploy.enabled values appear in the output.

### Testable Claims (EARS)
AC1.1: WHEN `factory apps` is invoked, THE CLI SHALL list every app under `apps/` that has a `config.yaml`, one row per app.
AC2.1: WHEN the command emits a row for a discovered app, THE CLI SHALL show at least the app name, repo, `self_tick_enabled`, and `deploy.enabled` for that app.
AC2.2: WHEN the command reads app configuration, THE CLI SHALL read effective values from the app's `config.yaml`.
AC3.1: WHEN `factory apps` is invoked, THE command SHALL be read-only.
AC3.2: WHEN at least one app is found, THE command SHALL exit 0.
AC4.1: WHEN unit test coverage is executed against a temporary `apps/` tree with two app configs, THE test SHALL invoke the command or its underlying pure list function.
AC4.2: WHEN the unit test inspects output from that temporary `apps/` tree, THE test SHALL assert both apps appear in the output.
AC4.3: WHEN the unit test inspects output from that temporary `apps/` tree, THE test SHALL assert both apps' `self_tick_enabled` and `deploy.enabled` values appear in the output.

# Tasks / Subtasks

- [x] Identify existing CLI entrypoint and subcommand registration path.
- [x] Identify existing YAML/config loading utilities reusable for `apps/*/config.yaml`.
- [x] Add narrow app-summary discovery/list function for `apps/*/config.yaml`.
- [x] Read only fields required by AC: app name, repo, `self_tick_enabled`, `deploy.enabled`.
- [x] Ensure values reflect effective values from each app `config.yaml`.
- [x] Wire new read-only `factory apps` CLI command.
- [x] Render one output row per discovered app.
- [x] Include required columns/fields in command output.
- [x] Return exit code 0 when at least one app is found.
- [x] Avoid any mutation of config, filesystem, or runtime state.
- [x] Add unit test using temp `apps/` tree with two app configs.
- [x] Assert both apps appear in output.
- [x] Assert both apps' `self_tick_enabled` values appear in output.
- [x] Assert both apps' `deploy.enabled` values appear in output.
- [x] Keep implementation minimal; no unrelated CLI refactor.

# Dev Notes

## Direction flow.md
(none)

## Direction api_spec.md
(none)

## Direction Acceptance Criteria (verbatim)
- [x] A new `factory apps` CLI command lists every app under apps/ that has a config.yaml, one row per app.
- [x] Each row shows at least: app name, repo, self_tick_enabled, and deploy.enabled (reading the effective values from the app's config.yaml).
- [x] The command is read-only — it never mutates any config or state — and exits 0 when at least one app is found.
- [x] A unit test invokes the command (or its underlying pure list function) against a temp apps/ tree with two app configs and asserts both apps and their self_tick_enabled / deploy.enabled values appear in the output.

## Context pointers
No canonical context files were provided in the prelude for this run.

## Implementation constraints
- Scope this story to a narrow read-only vertical slice only.
- Prefer existing CLI wiring and config parsing utilities over introducing new abstractions.
- Discovery scope is only `apps/*/config.yaml`.
- Output contract is minimal: one row per app with required fields present.
- If app config contains more data, do not expand scope beyond the listed fields.
- Because repo context is missing, Dev must derive concrete file paths and command registration points from the codebase before editing.
- Because no canonical context exists yet, Reviewer/Test-Designer should treat repository source as the primary truth for CLI structure and config loading behavior.

## Open repo-discovery items for Dev
- Locate the command root for `factory`.
- Locate any existing table/text output helper used by CLI commands.
- Locate YAML decoding path for app config files.
- Determine how effective values are currently computed, if helper logic already exists.
- Determine command behavior when zero apps are found; do not infer beyond the stated ACs.

# References

- Direction: `direction.md`
- PM decomposition source: `pm_result.child_stories[0]`
- Story target: `stories/61-add-a-factory-apps-cli-command-listing-configured-apps-and-k.md`

# Dev Agent Record

## Implementation Notes
- Added `list_apps()` pure function in `factory/app_config.py` that discovers `apps/*/config.yaml` via `Path.glob`, calls existing `load_app_config()` for each, and returns a list of dicts with exactly four keys: `name`, `repo`, `self_tick_enabled`, `deploy_enabled`.
- Added `factory apps` CLI command in `factory/cli.py` as a top-level `@app.command("apps")`. Uses Rich `Table` for output (consistent with other CLI commands). Read-only — no mutations anywhere. Exits 0 when apps are found; also exits 0 (with a dim message) when none are found.
- Test file `tests/test_cli_apps.py` covers: pure function returns both apps with required fields, idempotency/correctness, CLI output contains both apps and all required column headers, and exit code 0.

## Files Touched
- `factory/app_config.py` — added `list_apps()` function
- `factory/cli.py` — added `apps` command
- `tests/test_cli_apps.py` — new, 6 tests

## Commands Run
- `uv run --extra dev pytest tests/test_cli_apps.py -v` — 6 passed
- `uv run --extra dev pytest -q` — full suite green

# Senior Developer Review

- Pending.

# Review Follow-ups

- Pending.