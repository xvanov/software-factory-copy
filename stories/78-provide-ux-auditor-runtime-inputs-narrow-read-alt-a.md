# Story

## Title
Provide UX auditor runtime inputs — narrow read

## Story
As the scheduled UX audit pipeline,
I want scheduled UX audit input to carry at least one `flow.md` artifact plus app URL/runtime context,
so that the UX auditor receives concrete runtime inputs instead of guessing the target flow and environment.

## Scope
Backend. Narrow read: scheduler/input-builder path only. This story does not implement auditor citation/parsing behavior beyond ensuring the required artifacts and runtime context are present in scheduled UX audit input.

# Acceptance Criteria

- [x] Scheduled UX audit input includes at least one flow.md plus app URL/runtime context.
- [ ] UX auditor can reference concrete flow filenames and step numbers from supplied artifacts.

### Testable Claims (EARS)
AC1.1: WHEN a scheduled UX audit input is built, THE scheduled UX audit input SHALL include at least one `flow.md` artifact.
AC1.2: WHEN a scheduled UX audit input is built, THE scheduled UX audit input SHALL include app URL context.
AC1.3: WHEN a scheduled UX audit input is built, THE scheduled UX audit input SHALL include runtime context.
AC2.1: UNTESTABLE-AS-WRITTEN — this narrow-read story is scoped to transport/runtime-input plumbing only; the criterion does not specify the citation mechanism, output surface, or parser behavior required to prove reference to concrete flow filenames and step numbers.

# Tasks / Subtasks

- [x] Identify scheduled UX audit entrypoint and input-builder path in codebase.
- [x] Identify existing scheduled audit payload shape and transport boundary.
- [x] Add required `flow.md` artifact inclusion to scheduled UX audit input.
- [x] Add app URL context field(s) to scheduled UX audit input.
- [x] Add runtime context field(s) to scheduled UX audit input.
- [x] Ensure at least one concrete flow artifact is attached or embedded on scheduled UX audit execution.
- [x] Preserve backward compatibility for non-UX scheduled audits, if such path exists.
- [x] Add/extend unit tests for scheduled UX audit input builder.
- [x] Add/extend integration test covering scheduled UX audit payload contents.
- [x] Verify story scope excludes auditor citation/parsing changes.
- [x] Document exact payload/input fields touched in Dev Agent Record.

# Dev Notes

## Scope Boundary
- Implement only the scheduled UX audit input plumbing.
- Do not implement or modify auditor finding citation logic unless strictly required to keep existing interfaces compiling.
- If AC2 remains unmet after this slice, record that gap in Senior Developer Review / Follow-ups for the next slice.

## Flow Artifact
[flow.md: none]

## API Spec
[api_spec.md: none]

## Context Pointers
- No canonical context files were provided in the prelude.
- Repo context is currently unavailable; derive implementation details from inspected code paths only.
- If this run also includes onboarding/context generation elsewhere in the chain, prefer the generated canonical docs once available before coding.

## Direction Acceptance Criteria (verbatim)
- [x] Scheduled UX audit input includes at least one flow.md plus app URL/runtime context.
- [ ] UX auditor can reference concrete flow filenames and step numbers from supplied artifacts.

## Implementation Notes
- `factory.chain.scheduled_tasks._build_ux_auditor_context` now hard-requires at least one `flow.md` artifact for scheduled `ux_auditor` runs; it raises `ValueError` when none are available.
- Scheduled UX input now carries concrete flow artifact headings (`<direction>/flow.md`) and full flow content, so numbered steps remain visible to downstream slices.
- App URL context now includes parsed HTTP(S) URL candidates extracted from deploy health/smoke commands.
- Runtime context now explicitly includes UTC timestamp, software-factory root, target app, and scheduler transport metadata.
- Non-UX persona scheduling path remains unchanged.

# References

- Direction: `D009 provide-ux-auditor-runtime-inputs`
- PM tracker: `D009 provide-ux-auditor-runtime-inputs`
- Child-story decomposition context:
  - `D009 attach flow.md and app runtime context to scheduled UX audits`
  - `D009 make UX auditor cite flow filenames and step numbers`

# Dev Agent Record

## Agent Model Used
- OpenHands (GPT-5)

## Debug Log References
- `pytest -q tests/test_ux_auditor_input.py` (red-first: verified missing hard requirement for flow artifact)
- `pytest -q tests/test_ux_auditor_input.py` (green after implementation)
- `pytest -q tests/test_ux_auditor.py tests/test_persona_ux_auditor.py tests/test_scheduled_persona.py tests/test_persona_bug_hunter.py tests/test_persona_ralph.py tests/test_security.py`
- `uv run --extra dev pytest -q` (full suite green)

## Completion Notes List
- Added strict scheduled UX input guardrail: scheduled UX runs now fail early when no `flow.md` artifacts exist (AC1.1).
- Added URL extraction for deploy commands and included concrete app URL candidates in scheduled UX context payload (AC1.2).
- Kept runtime context fields in UX payload and asserted them in tests (AC1.3).
- Added/rewrote UX auditor input tests to cover unit and integration behavior of scheduler prompt construction and compatibility for non-UX runs.
- AC2 remains intentionally out of scope for this slice; citation/parsing behavior was not implemented.

## File List
- `factory/chain/scheduled_tasks.py`
- `tests/test_ux_auditor_input.py`
- `stories/78-provide-ux-auditor-runtime-inputs-narrow-read-alt-a.md`

# Senior Developer Review

- AC2 follow-up is still required in the citation/parser slice: prove the UX auditor outputs references to concrete filenames and step numbers.

# Review Follow-ups

- Next slice should define and test the exact output/citation contract (format + parser surface) for referencing flow filenames and step numbers.
