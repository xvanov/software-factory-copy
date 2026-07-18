---
title: Strengthen integration secret governance and log redaction
type: security
priority: p2
explore: true
created_at: '2026-07-17T23:12:26.671054+00:00'
---

<!-- Optional sibling files: flow.md (user flow), api_spec.md (API contract), artifacts/ (binaries) -->

# Strengthen integration secret governance and log redaction

## Why

Leaked integration credentials can lead to payment abuse and external account compromise.

## Acceptance Criteria

- [ ] Secrets are loaded from approved secure sources only, not defaults/hardcoded fallbacks
- [ ] Application logs redact tokens/keys and tests assert redaction behavior
- [ ] Documented rotation and scope policy exists for Stripe/OAuth/provider credentials
