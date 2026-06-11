---
title: Review and harden OAuth state and account-linking controls
type: security
priority: p2
explore: false
created_at: '2026-06-01T01:07:53.861859+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Review and harden OAuth state and account-linking controls

## Why

Federated login failures commonly lead to account takeover rather than isolated bugs.

## Acceptance Criteria

- [ ] OAuth callbacks reject missing or mismatched state/nonce and validate provider token claims.
- [ ] Account linking requires an existing authenticated session plus explicit confirmation, not automatic email-based merge.
