---
title: Constrain verifier SSRF and sandbox egress
type: security
priority: p2
explore: false
created_at: '2026-06-11T15:18:54.473379+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Constrain verifier SSRF and sandbox egress

## Why

User-controlled verification targets can otherwise reach internal infrastructure or pivot from worker execution.

## Acceptance Criteria

- [ ] Verifier blocks requests to private, loopback, link-local, and metadata IP ranges after DNS resolution and on redirect
- [ ] Sandboxed verification runs with restricted network and filesystem access and documented resource limits
- [ ] Tests cover representative SSRF bypass cases including redirects and DNS rebinding-style resolution changes
