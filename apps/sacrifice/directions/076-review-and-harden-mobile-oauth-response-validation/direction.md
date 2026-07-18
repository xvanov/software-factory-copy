---
title: Review and harden mobile OAuth response validation
type: security
priority: p2
explore: false
created_at: '2026-07-06T09:02:04.092544+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Review and harden mobile OAuth response validation

## Why

OAuth misbinding bugs can become account takeover even when passwords are never handled directly.

## Acceptance Criteria

- [ ] OAuth login/link flows document and enforce PKCE, state, and redirect URI validation.
- [ ] Backend validates token issuer, audience, expiry, and nonce/state before account binding.
- [ ] Negative tests cover replayed or mismatched OAuth responses.
