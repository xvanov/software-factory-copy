---
title: Remove dead-end goal creation paths
type: ux
priority: p2
explore: false
created_at: '2026-06-01T01:07:37.277836+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Remove dead-end goal creation paths

## Why

Users following documented flows for unsupported goal types cannot complete creation because the selector is hard-coded to four options.

## Acceptance Criteria

- [ ] Goal creation UI only advertises goal types that the backend accepts today, or newly documented types are fully supported end-to-end.
- [ ] A user following any published creation flow can find and select the required goal type.
- [ ] Unsupported goal types are not surfaced as selectable options without a clear unavailable state.
