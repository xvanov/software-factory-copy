---
title: Harden OAuth callback and mobile browser auth binding
type: security
priority: p2
explore: false
created_at: '2026-06-22T09:02:21.708201+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Harden OAuth callback and mobile browser auth binding

## Why

OAuth is a primary account-entry path and callback confusion can become account compromise.

## Acceptance Criteria

- [ ] All OAuth callbacks validate state/nonce against server-side session data
- [ ] Redirect URIs are enforced via explicit allowlist
- [ ] Tests cover failed state, reused state, and mismatched client-origin flows
