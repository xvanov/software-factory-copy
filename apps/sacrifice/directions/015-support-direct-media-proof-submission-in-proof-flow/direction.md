---
title: Support direct media proof submission in proof flow
type: ux
priority: p2
explore: false
created_at: '2026-06-01T01:07:37.275527+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Support direct media proof submission in proof flow

## Why

Users currently cannot complete proof flows with captured media because the submission surface only accepts JSON-style inputs such as pasted URLs.

## Acceptance Criteria

- [ ] Proof submission UI offers a direct media capture or file upload path for supported goal types.
- [ ] Client submits proof as multipart or equivalent binary-capable transport instead of application/json-only when media is attached.
- [ ] A user can complete the documented proof flow without leaving the app to host evidence elsewhere.
