---
title: Audit and redact secret exposure across runtime surfaces
type: security
priority: p2
explore: false
created_at: '2026-06-22T09:02:21.710831+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Audit and redact secret exposure across runtime surfaces

## Why

Multi-surface apps often leak credentials through logs and operational tooling.

## Acceptance Criteria

- [ ] Sensitive settings fields are redacted in logs and error serialization
- [ ] CLI and startup paths avoid printing raw environment/config values
- [ ] Tests or checks cover redaction behavior for configured secrets
