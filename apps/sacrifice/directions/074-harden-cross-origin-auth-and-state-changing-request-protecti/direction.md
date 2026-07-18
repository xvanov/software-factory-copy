---
title: Harden cross-origin auth and state-changing request protections
type: security
priority: p2
explore: false
created_at: '2026-07-06T09:02:04.090009+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Harden cross-origin auth and state-changing request protections

## Why

Session-bearing API calls are a primary web attack surface when CORS and CSRF assumptions drift.

## Acceptance Criteria

- [ ] CORS origin configuration is explicit and environment-scoped, not wildcard-based.
- [ ] Credentialed requests are only enabled when required by the chosen auth mechanism.
- [ ] State-changing routes are covered by CSRF defenses when cookie auth is enabled.
