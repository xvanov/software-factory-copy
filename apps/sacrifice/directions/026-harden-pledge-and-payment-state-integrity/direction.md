---
title: Harden pledge and payment state integrity
type: security
priority: p2
explore: false
created_at: '2026-06-11T15:18:54.472430+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Harden pledge and payment state integrity

## Why

Money movement and donation enforcement require strict server-side ownership and transition controls.

## Acceptance Criteria

- [ ] Server rejects payment method references not owned by the authenticated user
- [ ] Pledge amount and funding-critical fields become immutable after commitment
- [ ] Charge/donation execution is gated by validated state transitions and payment reconciliation
