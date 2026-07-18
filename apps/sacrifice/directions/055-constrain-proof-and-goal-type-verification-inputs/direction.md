---
title: Constrain proof and goal-type verification inputs
type: security
priority: p2
explore: false
created_at: '2026-06-22T09:02:21.709668+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Constrain proof and goal-type verification inputs

## Why

Verification correctness directly controls financial and trust outcomes.

## Acceptance Criteria

- [ ] Proof payloads are validated against goal-type-specific schemas with unknown fields rejected
- [ ] Server enforces goal ownership and valid lifecycle state before verification
- [ ] Tests cover malformed proof payloads and cross-user proof submission attempts
