# Story
D078 add unauthenticated /healthz route reusing health handler

## Acceptance Criteria
- AC1: GET /healthz returns 200 with JSON body {"status": "ok"}
- AC2: GET /healthz requires no authentication
- AC3: A backend test covers GET /healthz returning 200 and status ok
- AC4: Existing GET /api/health continues to return 200

## Tasks / Subtasks
- [ ] Inspect current health route wiring in backend app entrypoints and routers.
- [ ] Implement unauthenticated GET /healthz route.
- [ ] Reuse existing /api/health handler logic rather than duplicating response behavior.
- [ ] Preserve existing GET /api/health behavior.
- [ ] Add backend test for GET /healthz -> 200 and {"status": "ok"}.
- [ ] Verify GET /healthz test exercises no-auth access.
- [ ] Verify existing GET /api/health still returns 200 in backend coverage.

## Dev Notes
### flow.md
(none)

### api_spec.md
# API spec

GET /healthz
  Response 200: {"status": "ok"}
  No authentication required (liveness probe).
  Reuses the existing /api/health handler logic.

### Context pointers
- [Source: context/project.md#Identity]
- [Source: context/project.md#Active constraints]

### Direction acceptance criteria (verbatim)
- [ ] GET /healthz returns 200 with JSON body {"status": "ok"}
- [ ] GET /healthz requires no authentication
- [ ] A backend test covers GET /healthz returning 200 and status ok
- [ ] Existing GET /api/health continues to return 200

### Implementation notes
- Navigation listed backend/current-state/module files, but only `context/project.md` and `context/navigation.md` were present in the provided prelude; do not assume missing files exist.
- Direction scope is backend only; no frontend, infra, or docs changes.
- Preserve existing route consumers on `/api/health` while adding `/healthz` for deploy liveness checks.

## References
- Direction: D078 Add /healthz liveness endpoint matching the deploy health check
- PM tracker: D078 add /healthz liveness endpoint for deploy health check
- Target story path: stories/0-d078-add-unauthenticated-healthz-route-reusing-health-handle.md

## Dev Agent Record
- Status: Not started
- Agent Model: 
- Debug Log References: 
- Completion Notes: 
- File List: 

## Senior Developer Review
- Reviewer: 
- Outcome: Pending
- Review Notes: 

## Review Follow-ups
- [ ] None yet
