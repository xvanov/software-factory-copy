---
title: Enforce object-level authorization on goal and proof endpoints
type: security
priority: p2
explore: false
created_at: '2026-06-01T01:07:53.859995+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Enforce object-level authorization on goal and proof endpoints

## Why

Accountability records are user-sensitive and must not be readable or mutable across tenants.

## Acceptance Criteria

- [ ] Every goal/proof endpoint verifies the current user is authorized for the referenced object.
- [ ] Cross-user access tests fail closed for read, submit-proof, and update flows.
