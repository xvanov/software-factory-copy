---
title: Add abuse controls around proof and verification endpoints
type: security
priority: p2
explore: false
created_at: '2026-07-06T09:02:04.093626+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Add abuse controls around proof and verification endpoints

## Why

Unbounded JSON and asynchronous work queues are a straightforward DoS vector.

## Acceptance Criteria

- [ ] Request size limits are enforced on goal creation and proof submission endpoints.
- [ ] Rate limits exist for verification-triggering routes.
- [ ] Background verification tasks have timeouts and bounded retry behavior.
