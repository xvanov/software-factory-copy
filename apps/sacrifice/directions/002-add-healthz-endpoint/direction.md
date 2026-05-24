---
title: Add healthz endpoint
type: feature
priority: p2
explore: false
created_at: '2026-05-24T17:13:19.361833+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Add healthz endpoint

## Why

Smoke test wants a stable endpoint to verify the service is alive.

## Acceptance Criteria

- [ ] Endpoint returns 200
- [ ] Body contains version + status
