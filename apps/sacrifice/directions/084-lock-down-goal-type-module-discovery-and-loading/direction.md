---
title: Lock down goal-type module discovery and loading
type: security
priority: p2
explore: true
created_at: '2026-07-17T23:12:26.667910+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Lock down goal-type module discovery and loading

## Why

Dynamic module loading without strict trust controls expands supply-chain and insider attack surface.

## Acceptance Criteria

- [ ] Goal-type registry only loads allowlisted modules from trusted paths
- [ ] Startup fails if module integrity checks or interface validation fail
- [ ] Security logging records module load decisions and verifier exceptions
