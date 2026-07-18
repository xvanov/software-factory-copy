---
title: Harden auth token lifecycle and replay defenses
type: security
priority: p2
explore: true
created_at: '2026-07-17T23:12:26.661405+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Harden auth token lifecycle and replay defenses

## Why

Compromise of bearer material directly enables account impersonation and downstream pledge abuse.

## Acceptance Criteria

- [ ] Access and refresh token policy documented and enforced in backend auth middleware
- [ ] Refresh token rotation with revocation implemented and tested for replay attempts
- [ ] Protected endpoints reject tokens with invalid issuer/audience/expiry
