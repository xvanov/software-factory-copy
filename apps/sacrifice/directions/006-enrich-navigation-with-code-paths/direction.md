---
title: Enrich context/navigation.md with code paths alongside context files
type: docs
priority: p3
explore: false
created_at: 2026-05-25
---

## Why

The current `context/navigation.md` on Sacrifice (merged in PR #23) only lists context files per task scope. PR #24's alternative listed actual code paths alongside the context references, which is dramatically more useful as an "open these to start working" map for future agents and humans. This direction harvests that pattern.

## Acceptance Criteria

- Every `## When working on X` section in `~/sacrifice/context/navigation.md` lists BOTH context files AND code files relevant to that task scope.
- Code references use repo-relative paths (e.g. `backend/app/routes/goals.py`, not absolute paths).
- Code references are accurate (the files exist and are relevant to the named task scope).
- The list of task scopes is unchanged (no new sections; no sections removed).
- File is rewritten as one canonical edit (no append-only artifacts).
- Existing canonical-paths enforcer must pass: only `context/navigation.md` touched.
- No code outside `context/` is modified.
