---
title: Constrain untrusted outbound fetch and sandbox execution paths
type: security
priority: p2
explore: false
created_at: '2026-06-01T01:07:53.860924+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Constrain untrusted outbound fetch and sandbox execution paths

## Why

User-controlled verification targets can be abused to reach internal services or escape weakly isolated execution environments.

## Acceptance Criteria

- [ ] Verification code rejects private, link-local, loopback, and non-HTTP(S) destinations where not explicitly required.
- [ ] Sandboxed execution runs with documented isolation, no privileged mounts, and bounded CPU/memory/network access.
