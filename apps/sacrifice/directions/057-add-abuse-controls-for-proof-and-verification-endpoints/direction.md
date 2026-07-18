---
title: Add abuse controls for proof and verification endpoints
type: security
priority: p2
explore: false
created_at: '2026-06-22T09:02:21.711941+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Add abuse controls for proof and verification endpoints

## Why

Unbounded JSON and downstream verification create a straightforward DoS path.

## Acceptance Criteria

- [ ] Proof-related endpoints reject oversized or deeply nested payloads
- [ ] External verification paths have explicit timeout and concurrency limits
- [ ] Rate limiting or equivalent abuse controls protect public-facing API routes
