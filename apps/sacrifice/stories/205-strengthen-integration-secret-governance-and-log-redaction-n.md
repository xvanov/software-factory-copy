# Story

## Title
Strengthen integration secret governance and log redaction — narrow read

## Story
As a backend maintainer,
I want backend configuration to reject insecure secret-loading paths and backend logging to redact tokens/keys,
so that integration credentials are not accepted from hardcoded/default fallbacks and do not leak through application logs.

## Scope
Backend-only narrow read covering code-path hardening for secret sourcing and runtime log redaction. Excludes standalone docs deliverable beyond implementation notes and excludes separate test-only story decomposition.

# Acceptance Criteria

- [ ] Secrets are loaded from approved secure sources only, not defaults/hardcoded fallbacks
- [ ] Application logs redact tokens/keys and tests assert redaction behavior

### Testable Claims (EARS)
AC1.1: WHEN backend settings are loaded, THE configuration layer SHALL accept secrets from approved secure sources only
AC1.2: WHEN backend settings encounter defaults or hardcoded fallback secret values, THE configuration layer SHALL reject those secret-loading paths
AC2.1: WHEN application logging emits tokens or keys, THE logging layer SHALL redact the sensitive values
AC2.2: WHEN redaction behavior is exercised by automated tests, THE test suite SHALL assert the redaction behavior

# Tasks / Subtasks

- [ ] Audit secret-bearing settings in `backend/app/config.py`
- [ ] Identify current default, fallback, or hardcoded secret-loading paths
- [ ] Define approved secure-source rule at backend settings boundary
- [ ] Enforce rejection/failure for disallowed secret defaults or fallbacks
- [ ] Preserve non-secret configuration behavior unless blocked by the new rule
- [ ] Add or update focused backend tests for approved secret-source enforcement
- [ ] Identify logger entry points that can emit tokens, keys, headers, or DSNs
- [ ] Implement log redaction at the backend logging boundary
- [ ] Ensure redaction covers structured and plain-message logging paths used by the app
- [ ] Add or update backend tests that assert masked output rather than raw secret values
- [ ] Verify no approved behavior relies on logging raw secrets
- [ ] Update story record sections during implementation

# Dev Notes

## Direction acceptance criteria (verbatim)
- [ ] Secrets are loaded from approved secure sources only, not defaults/hardcoded fallbacks
- [ ] Application logs redact tokens/keys and tests assert redaction behavior
- [ ] Documented rotation and scope policy exists for Stripe/OAuth/provider credentials

## flow.md
(none)

## api_spec.md
(none)

## Context pointers
- [Source: context/project.md#Identity]
- [Source: context/project.md#Stack]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on backend API or goal lifecycle]

## Implementation notes
- Primary code surface is `backend/app/config.py` per PM notes.
- Integration set called out by current context: Google OAuth, GitHub OAuth, YouTube, Stripe, Redis, PostgreSQL, and Azure Foundry.
- Direction requires executable proof for redaction behavior; include tests in scope for this narrow backend read.
- No `context/current-state.md`, `context/modules/backend.md`, or `context/glossary.md` content was provided in the prelude; do not cite absent sections.
- No `flow.md` or `api_spec.md` content exists for verbatim embedding in this direction.
- Docs acceptance criterion is intentionally out of scope for this narrow backend story and is expected to land in the separate docs child story.

# References

- `backend/app/config.py`
- `backend/app/main.py`
- `backend/pyproject.toml`
- PM tracker: `D085 strengthen secret governance and log redaction`
- Direction: `Strengthen integration secret governance and log redaction`

# Dev Agent Record

## Agent Model Used
- TBD

## Debug Log References
- TBD

## Completion Notes List
- TBD

## File List
- TBD

# Senior Developer Review

- TBD

# Review Follow-ups

- TBD
