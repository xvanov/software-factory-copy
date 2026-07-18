---
title: Bind proof artifacts to goal and submission window
type: security
priority: p2
explore: false
created_at: '2026-06-11T15:18:54.474339+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Bind proof artifacts to goal and submission window

## Why

Loose JSON/URL proof submission is vulnerable to replay and unrelated-evidence abuse.

## Acceptance Criteria

- [ ] Verification checks prove the submitted artifact matches the goal owner and deadline requirements
- [ ] Fetched third-party proof metadata is snapshotted and compared against submission-time rules
- [ ] Higher-assurance goal types have a documented path away from unauthenticated pasted URLs
