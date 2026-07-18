---
title: Add CSRF protections to cookie-authenticated API flows
type: security
priority: p2
explore: false
created_at: '2026-06-11T15:18:54.471363+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Add CSRF protections to cookie-authenticated API flows

## Why

Credentialed cross-origin browser access materially raises the risk of forged authenticated actions.

## Acceptance Criteria

- [ ] All state-changing cookie-authenticated endpoints reject requests without a valid CSRF token or equivalent protection
- [ ] Auth cookie attributes are explicitly configured with Secure, HttpOnly, and an appropriate SameSite policy
- [ ] Frontend auth flow documents and uses the chosen CSRF-safe pattern
