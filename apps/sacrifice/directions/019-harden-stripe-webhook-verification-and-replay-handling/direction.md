---
title: Harden Stripe webhook verification and replay handling
type: security
priority: p2
explore: false
created_at: '2026-06-01T01:07:53.859058+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Harden Stripe webhook verification and replay handling

## Why

Financial state must only change in response to authenticated, fresh Stripe events.

## Acceptance Criteria

- [ ] Webhook handlers reject invalid, missing, or stale Stripe signatures.
- [ ] Processed event IDs are stored to prevent replay-driven duplicate state transitions.
