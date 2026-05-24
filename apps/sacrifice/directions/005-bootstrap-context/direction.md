---
title: Bootstrap Sacrifice canonical context
type: docs
priority: p1
explore: true
created_at: '2026-05-24T13:30:00+00:00'
---

# Bootstrap Sacrifice canonical context

## Why

`~/sacrifice/` has no canonical `context/` set yet. Until it does, every
persona invocation runs against the `NO CONTEXT AVAILABLE` notice and the
chain cannot route work intelligently. The Onboarder persona produces the
canonical context once, in a single pass, by reading the existing
Sacrifice repo (legacy READMEs, AGENTS.md, code layout, package manifests)
and writing the canonical context set.

## Acceptance Criteria

- [ ] `~/sacrifice/context/project.md` exists and identifies app + stack
- [ ] `~/sacrifice/context/current-state.md` records active architectural decisions in current-tense prose
- [ ] `~/sacrifice/context/architecture-diagrams.md` contains at least one mermaid system diagram and one mermaid sequence diagram for the primary user flow
- [ ] `~/sacrifice/context/navigation.md` maps task scopes to the appropriate canonical files
- [ ] `~/sacrifice/context/glossary.md` defines every domain term that appears in module names or user-facing surfaces (no generic software terms)
- [ ] `~/sacrifice/context/sprint-status.yaml` is the BMAD stub form (`current_sprint: null`, etc.)
- [ ] `~/sacrifice/context/modules/<name>.md` exists for every current module the Onboarder discovered
- [ ] No file outside CANONICAL_CONTEXT_PATHS is created; no file matches FORBIDDEN_DOC_PATTERNS
- [ ] The canonical-paths enforcer reports zero violations against the resulting PR
