---
title: Constrain dynamic goal-type loading in production
type: security
priority: p2
explore: false
created_at: '2026-07-06T09:02:04.091457+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Constrain dynamic goal-type loading in production

## Why

Filesystem auto-discovery turns plugin addition into arbitrary backend code execution.

## Acceptance Criteria

- [ ] Production goal-type loading uses an explicit allowlist or signed manifest.
- [ ] Registry import paths reject unexpected modules outside approved packages.
- [ ] Goal-type modules are audited to avoid import-time side effects.
