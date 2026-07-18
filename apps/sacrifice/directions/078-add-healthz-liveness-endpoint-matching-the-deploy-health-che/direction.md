---
title: Add /healthz liveness endpoint matching the deploy health check
type: bug
priority: p2
explore: false
created_at: '2026-07-07T04:52:19.151904+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Add /healthz liveness endpoint matching the deploy health check

## Why

The deploy config health-checks GET /healthz (apps/sacrifice/config.yaml deploy.health_check_command), but the backend only serves GET /api/health — so a real deploy can never verify liveness and would roll back. Add a /healthz endpoint that returns liveness so the deploy gate works.

## Acceptance Criteria

- [ ] GET /healthz returns 200 with JSON body {"status": "ok"}
- [ ] GET /healthz requires no authentication
- [ ] A backend test covers GET /healthz returning 200 and status ok
- [ ] Existing GET /api/health continues to return 200
