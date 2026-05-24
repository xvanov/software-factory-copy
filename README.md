# software-factory

Phase 0 foundation: runner, model router, dev persona, canonical context loader.

This is the orchestrator. The OpenHands SDK is the execution substrate (consumed as a pip dep), BMAD personas are the prompt sources (substance ported, interactive chrome stripped).

## Quickstart

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Run tests
make test

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

