---
title: Reduce dead-end states in goal type selection
type: ux
priority: p2
explore: false
created_at: '2026-05-30T15:23:18.933321+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Reduce dead-end states in goal type selection

## Why

Hard-coded goal types create a selection step where unsupported user intents have no recoverable path.

## Acceptance Criteria

- [ ] Goal creation either explains supported goal-type limits inline before commitment or offers a fallback/custom path for unsupported goals.
