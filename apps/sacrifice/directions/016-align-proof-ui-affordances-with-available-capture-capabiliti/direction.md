---
title: Align proof UI affordances with available capture capabilities
type: ux
priority: p2
explore: false
created_at: '2026-06-01T01:07:37.276811+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Align proof UI affordances with available capture capabilities

## Why

The app cannot currently honor in-app capture expectations because native media capabilities are not configured.

## Acceptance Criteria

- [ ] Expo configuration includes the plugins and permissions needed for intended proof capture features.
- [ ] Proof screens do not present camera-dependent actions unless the capability is available.
- [ ] Documented proof flows that mention capture can be executed end-to-end in the app shell.
