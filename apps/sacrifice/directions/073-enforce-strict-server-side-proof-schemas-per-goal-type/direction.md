---
title: Enforce strict server-side proof schemas per goal type
type: security
priority: p2
explore: false
created_at: '2026-07-06T09:02:04.088166+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Enforce strict server-side proof schemas per goal type

## Why

Generic JSON proof acceptance makes verifier implementations a high-risk trust boundary.

## Acceptance Criteria

- [ ] Proof submissions are validated against a goal-type-specific schema before persistence or verification.
- [ ] Unknown proof fields are rejected with a 4xx response.
- [ ] Tests cover malformed and over-permissive proof payloads for each built-in goal type.
