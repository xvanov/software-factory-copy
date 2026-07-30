# Story

## Title
Attach gate findings to UX audit inputs — broad read

## Slug
`attach-gate-findings-to-ux-audit-inputs-broad-read-alt-b`

## Scope
`test`

## Summary
Prepare the reproducible `tests-meaningful` finding artifact path that scheduled UX audit input can consume, with test-facing coverage over artifact fields and audit-input inclusion behavior.

# Acceptance Criteria

- [x] Scheduled UX audit input includes reproducible `tests-meaningful` finding artifacts showing rule id, file, line, and remediation text.

### Testable Claims (EARS)
AC1.1: WHEN the scheduled UX audit input is generated, THE audit input SHALL include reproducible `tests-meaningful` finding artifacts
AC1.2: WHEN a `tests-meaningful` finding artifact is included in scheduled UX audit input, THE artifact SHALL show rule id
AC1.3: WHEN a `tests-meaningful` finding artifact is included in scheduled UX audit input, THE artifact SHALL show file
AC1.4: WHEN a `tests-meaningful` finding artifact is included in scheduled UX audit input, THE artifact SHALL show line
AC1.5: WHEN a `tests-meaningful` finding artifact is included in scheduled UX audit input, THE artifact SHALL show remediation text

# Tasks / Subtasks

- [x] Identify the existing scheduled UX audit input assembly path exercised by this direction
- [x] Identify the current `tests-meaningful` finding source or nearest reproducible fixture seam
- [x] Add or update a reproducible test fixture for `tests-meaningful` findings containing rule id, file, line, and remediation text
- [x] Add test coverage asserting the fixture output remains reproducible across runs
- [x] Add test coverage asserting scheduled UX audit input includes the finding artifact payload
- [x] Add test coverage asserting the included artifact exposes rule id, file, line, and remediation text
- [x] Keep implementation scope limited to test-enabling and verification changes needed for audit-input attachment behavior
- [x] Record exact file paths and commands used in Dev Agent Record

# Dev Notes

## Flow Embed

# User flow

1. Flow: 014-detect-tests-that-bypass-the-app-entry-point/flow.md
2. Step: 2
3. Evidence: Step requires observing PR gate output (`tests-meaningful` red) naming file and line, but current invocation is `text_run` with no CI/PR surface, browser access, or captured gate artifact attached to the prompt.
4. Suggestion: Expose CI finding artifacts or a reproducible local gate command output to the audit so message clarity can be checked against the documented expectation.

## API Spec Embed

(none)

## Context Pointers

- No canonical context files were provided in this invocation.
- No `context/project.md` available.
- No `context/navigation.md` available.
- No `context/current-state.md` available.
- No `context/modules/*.md` files available.
- Dev must derive file-level implementation context from the repository code paths that contain scheduled UX audit input assembly and `tests-meaningful` gate output generation.
- Test-Designer should inspect the same implementation paths and any existing fixtures covering audit-input prompts, gate findings, or text-run payload assembly.

## Direction Acceptance Criteria (Verbatim)

- [x] Scheduled UX audit input includes reproducible `tests-meaningful` finding artifacts showing rule id, file, line, and remediation text.

## Direction/PM Alignment Notes

- PM decomposition context indicates this story is the first test-scoped slice: "Add a reproducible artifact producer for `tests-meaningful` findings that captures the required fields."
- This broad-read story must still validate the end requirement against scheduled UX audit input, not only fixture shape in isolation.
- `api_spec.md` is explicitly `(none)` in the direction.
- Because no canonical repo context was supplied, any ambiguity discovered in artifact source, audit-input builder, or fixture format must be surfaced explicitly in implementation notes and review.

# References

- `direction.md` — Attach gate findings to UX audit inputs
- `flow.md` — embedded verbatim in Dev Notes
- `api_spec.md` — `(none)`
- PM tracker title: `D017 attach gate findings to UX audit inputs`
- PM child story context: `D017 add reproducible tests-meaningful finding artifact fixture`

# Dev Agent Record

## Agent Model Used
- openhands

## Debug Log References
- `uv sync --all-extras`
- `uv run pytest tests/test_ux_auditor_input.py::test_collect_tests_meaningful_findings_uses_repo_relative_paths -q` — fails first (red-first check)
- `uv run pytest tests/test_ux_auditor_input.py -q` — passes
- `uv run pytest tests/test_acceptance_oracle.py::test_gate_fails_on_ac_violation_even_when_dev_tests_green tests/test_ears_property_oracle.py::test_property_oracle_fails_on_violation_even_when_dev_tests_green tests/test_gates_evaluation.py::test_tests_meaningful_ablation_fails_on_unexercised_symbol -q` — passes
- `uv run pytest -q` — full suite passes (with expected warnings, 3 skipped)

## Completion Notes List
- `_collect_tests_meaningful_findings(app, software_factory_root)` in `factory/chain/scheduled_tasks.py` now normalizes each finding `path` to a POSIX path relative to the app repo root, then sorts findings by `(path, line)` for deterministic reproducibility.
- Scheduled UX auditor context assembly (`_build_ux_auditor_context`) includes a `### Gate Findings` section that emits `tests-meaningful` artifacts with rule id (`kind`), file (`path`), line, and remediation text (`why_slop`).
- `tests/test_ux_auditor_input.py` covers collector reproducibility, repo-relative path stability, inclusion of gate finding payload in scheduled UX context, and all required artifact fields.
- Scope stayed within test-enabling behavior for audit-input attachment; no additional product behavior was introduced.

## File List
- `factory/chain/scheduled_tasks.py` — normalized `tests-meaningful` finding paths to repo-relative values and sorted findings for reproducibility
- `tests/test_ux_auditor_input.py` — added repo-relative path reproducibility assertion; retained AC coverage for context inclusion and required fields
- `stories/164-attach-gate-findings-to-ux-audit-inputs-broad-read-alt-b.md` — updated completion checklists and Dev Agent Record

# Senior Developer Review

- [x] Story scope stayed within `test`
- [x] Reproducible artifact source identified and exercised by tests
- [x] Scheduled UX audit input inclusion verified by tests
- [x] Artifact fields verified: rule id, file, line, remediation text
- [x] No requirements added beyond direction AC
- [x] Any missing repository context captured explicitly

# Review Follow-ups

- [ ] TBD