# software-factory

Phase 0 foundation: runner, model router, dev persona, canonical context loader.

This is the orchestrator. The OpenHands SDK is the execution substrate (consumed as a pip dep), BMAD personas are the prompt sources (substance ported, interactive chrome stripped).

## Quickstart

```bash
# Sync runtime + dev deps (pytest, ruff, mypy live under the `dev` extra —
# a bare `uv sync` will leave them out and `uv run pytest` will fail with
# `ModuleNotFoundError: No module named 'pytest'`).
uv sync --all-extras

# Run tests (uv auto-activates .venv; manual `source .venv/bin/activate`
# is not required when you go through `uv run`).
uv run pytest -q

# Run the Phase 0 acceptance target
factory test-persona dev \
  --story ~/factory-test/story.md \
  --repo ~/factory-test \
  --dry-run
```

See `factory/cli.py` for available commands.

## Phase 6 autonomous-work scheduling

The Phase 6 personas (Ralph, Bug-Hunter, Security, UX-Auditor) are designed to
run on a cron. The factory does **not** install crontab entries for you — drop
the following into `crontab -e` (or your system's cron-equivalent) once you're
comfortable with the dry-run output:

```cron
# Ralph — hourly spec-vs-reality diff
0 * * * * cd ~/software-factory && .venv/bin/factory ralph-now --app sacrifice

# Bug-Hunter — daily security/quality scan at 04:00
0 4 * * * cd ~/software-factory && .venv/bin/factory bug-hunt-now --app sacrifice

# UX-Auditor — daily Playwright drive at 05:00
0 5 * * * cd ~/software-factory && .venv/bin/factory ux-audit-now --app sacrifice

# Security — weekly deeper audit on Monday at 09:00 UTC
0 9 * * 1 cd ~/software-factory && .venv/bin/factory security-now --app sacrifice
```

Each entry consults the per-persona daily-run cap in
`factory_settings.yaml::rate_limits` (`ralph_runs_per_day`,
`bug_hunter_runs_per_day`, `security_runs_per_day`, `ux_auditor_runs_per_day`)
before invoking the persona; runs that exceed the cap are refused with
`rejected_reason=<persona>_rate_limit_exceeded` and no LLM call happens.

Use `--dry-run` to exercise the chain end-to-end without an API key:

```bash
factory ralph-now --app sacrifice --dry-run
factory bug-hunt-now --app sacrifice --dry-run
factory security-now --app sacrifice --dry-run
factory ux-audit-now --app sacrifice --dry-run
```

## Phase 7 polish: inbox, status, idle, dual-draft

Phase 7 adds the operator-facing polish layer on top of the autonomous
loop. Every command supports `--dry-run` for offline iteration.

### Multi-app inbox

`factory inbox` aggregates across every `apps/<name>/`:

* Stories awaiting human action (`reviewer_requested_changes`,
  `blocked_tests_need_clarification`, or any `last_rejection_reason`).
* Directions in `needs-direction`.
* Budget warnings when today's spend ≥ 75% of the daily cap.
* Failed deploys in the last 24h.
* Active Direction Trackers.
* Recent scheduled persona runs (last 24h).
* Idle apps (no work in flight, no recent findings, no recent deploys).
* Pinned `factory-status` issue numbers per app.

Pass `--app <name>` to restrict to a single app.

### Pinned factory-status GitHub issue (per app)

`factory status-sync --app sacrifice` opens (or updates) one GH issue
per app, labeled `factory-status`, titled `[FACTORY] <app> live status`.
The body carries the current mode, queue depth, today's spend, last 5
deploys, active blockers, and active Direction Trackers. Idempotent.

```bash
# Dry-run — print the body without touching GH.
factory status-sync --app sacrifice --dry-run

# Real-run — requires GITHUB_TOKEN.
factory status-sync --app sacrifice
```

### Idle detection + `factory-idle` issue

When an app has no in-flight work AND no scheduled persona findings AND
no deploys for the lookback window (default 2h), the factory opens a
`factory-idle` issue listing the last 5 directions for context.
Re-running while the issue is open updates the body — no duplicates.

```bash
# Dry-run — print the snapshot when idle, or "not idle" otherwise.
factory idle-check --app sacrifice --dry-run

# Real-run.
factory idle-check --app sacrifice
```

### Dual-draft PRs (rare ambiguity workflow)

When a direction has the `(explore)` tag in frontmatter OR PM
confidence < 0.6, `handle_stories_spawned` produces two
StoryRecords with materially different interpretations (one per branch
`story/<n>-<slug>-alt-a` and `story/<n>-<slug>-alt-b`). Each flows
through the TDD chain independently → two `draft-alternative` PRs land
plus a comparison comment on the Direction Tracker. No new CLI; fires
automatically inside `factory pm-sync` when applicable.

### Recommended cron entries (Phase 7)

```cron
# Pinned factory-status issue — every 5 minutes.
*/5 * * * * cd ~/software-factory && .venv/bin/factory status-sync --app sacrifice

# Idle detection — every 30 minutes.
*/30 * * * * cd ~/software-factory && .venv/bin/factory idle-check --app sacrifice
```

### `_factory_improver` stub persona (v2-dormant)

`factory/personas/_factory_improver.md` is a v2 placeholder for a
future self-improvement persona. **Not invocable in v1.** It exists so
a v2 agent that adds `apps/software-factory/` later has a starting
point. The chain has no handler that routes work to it.

