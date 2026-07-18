---
title: Audit and harden OAuth login and account linking
type: security
priority: p2
explore: false
created_at: '2026-06-11T15:18:54.475339+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Audit and harden OAuth login and account linking

## Why

Multi-provider auth across web/mobile commonly fails at callback, state, and account-link boundaries.

## Acceptance Criteria

- [ ] OAuth callbacks validate state, issuer, audience, and exact redirect URI
- [ ] Account linking requires an authenticated session plus explicit confirmation
- [ ] Tests cover login CSRF, duplicate-email/provider collision, and callback-mixup cases
