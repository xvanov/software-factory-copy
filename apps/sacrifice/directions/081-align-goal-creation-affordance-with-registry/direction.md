---
title: Align goal creation affordance with registry
type: ux
priority: p2
explore: true
created_at: '2026-07-17T23:12:10.833361+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Align goal creation affordance with registry

## Why

A visible goal creation entry point that excludes registered types misleads users about what actions are actually available.

## Acceptance Criteria

- [ ] The set of selectable goal types in the UI matches the registry-backed set accepted by the API.
- [ ] Attempting to create a registered goal type does not fail because of stale client or schema unions.
