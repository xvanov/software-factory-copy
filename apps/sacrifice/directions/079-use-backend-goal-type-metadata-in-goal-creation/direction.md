---
title: Use backend goal-type metadata in goal creation
type: ux
priority: p2
explore: true
created_at: '2026-07-17T23:12:10.826878+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Use backend goal-type metadata in goal creation

## Why

Hardcoded goal-type options create a dead-end where valid backend-supported goal types are absent from the user flow.

## Acceptance Criteria

- [ ] Goal creation fetches and renders options from /api/goal-types instead of local constants.
- [ ] A backend-registered goal type appears in the picker without changing frontend source lists.
