---
title: Enforce strict proof schema and state-transition validation
type: security
priority: p2
explore: true
created_at: '2026-07-17T23:12:26.664757+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Enforce strict proof schema and state-transition validation

## Why

Weak proof validation can be abused to avoid legitimate pledge charging and undermines platform trust.

## Acceptance Criteria

- [ ] Server validates proof payload against goal-type-specific schema before persistence
- [ ] Illegal proof/status transitions are rejected with test coverage
- [ ] Audit events capture rejected and accepted proof validation outcomes
