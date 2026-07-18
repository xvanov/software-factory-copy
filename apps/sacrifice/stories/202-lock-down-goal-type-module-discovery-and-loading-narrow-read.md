# Story

## Story
As a backend maintainer,
I want goal-type discovery restricted to allowlisted modules from trusted paths,
so that dynamic module loading does not expand the app's runtime trust boundary.

## Acceptance Criteria
- [ ] Goal-type registry only loads allowlisted modules from trusted paths

### Testable Claims (EARS)
AC1.1: WHEN the goal-type registry discovers candidate modules, THE goal-type registry SHALL load only modules that are allowlisted and located on trusted paths
AC1.2: WHEN the goal-type registry discovers a candidate module that is not allowlisted, THE goal-type registry SHALL not load that module
AC1.3: WHEN the goal-type registry discovers a candidate module that is not located on a trusted path, THE goal-type registry SHALL not load that module

## Tasks / Subtasks
- [ ] Identify current discovery entrypoints in `backend/app/goal_types/registry.py`
- [ ] Define repo-local allowlist source of truth for eligible goal-type modules
- [ ] Define trusted-path check anchored to repository-owned goal-type locations
- [ ] Update discovery logic to require both allowlist membership and trusted-path match
- [ ] Preserve loading of existing built-in goal types that satisfy the new policy
- [ ] Reject or skip discovered modules that fail allowlist or trusted-path checks
- [ ] Add backend tests covering allowlisted trusted modules loading successfully
- [ ] Add backend tests covering non-allowlisted modules not loading
- [ ] Add backend tests covering untrusted-path modules not loading
- [ ] Add backend tests proving the policy applies during filesystem discovery, not only at dispatch time

## Dev Notes
- Scope boundary: narrow read of the direction. This story implements only the discovery trust policy. Startup fail-fast behavior and security logging are deferred to later child stories from `pm_result.child_stories`.
- No `flow.md` provided by direction.
- No `api_spec.md` provided by direction.
- Acceptance criteria available from direction; only AC1 is in scope for this story.

### Direction Acceptance Criteria (verbatim)
- [ ] Goal-type registry only loads allowlisted modules from trusted paths
- [ ] Startup fails if module integrity checks or interface validation fail
- [ ] Security logging records module load decisions and verifier exceptions

### Context pointers
- [Source: context/project.md#Identity]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on backend API or goal lifecycle]

### Implementation pointers
- Existing dynamic discovery seam already exists in backend goal-type registry; story work should constrain that seam rather than redesign it wholesale. [Source: context/project.md#Identity]
- Current context explicitly states auto-discovery from filesystem is live code behavior and is the attack surface for this direction. [Source: context/project.md#Identity]
- Keep the policy repo-local and minimal; do not invent broader product behavior beyond allowlisted modules from trusted paths.
- Tests should exercise discovery outcomes through the registry contract used by goal routes, without expanding into startup boot failure or logging assertions.

## References
- `backend/app/goal_types/registry.py`
- `backend/app/routes/goals.py`
- `backend/app/schemas/goal.py`
- `backend/app/models/goal.py`
- `backend/app/main.py`
- `backend/tests/` 
- `context/project.md`
- `context/navigation.md`

## Dev Agent Record
- Agent Model Used: 
- Debug Log References: 
- Completion Notes: 
- File List: 

## Senior Developer Review
- Reviewer: 
- Review Date: 
- Outcome: 
- Notes: 

## Review Follow-ups
- [ ] None recorded
