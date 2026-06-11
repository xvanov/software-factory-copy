---
title: Add CSRF protections to cookie-authenticated API routes
type: security
priority: p2
explore: false
created_at: '2026-06-01T01:07:53.858019+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Add CSRF protections to cookie-authenticated API routes

## Why

Cross-origin app access plus session auth creates account-action forgery risk unless anti-CSRF controls are explicit.

## Acceptance Criteria

- [ ] All state-changing authenticated routes reject requests without a valid CSRF token or equivalent protection.
- [ ] Session cookie settings are reviewed and hardened for SameSite, Secure, and HttpOnly semantics.
