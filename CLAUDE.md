# CLAUDE.md — factory orchestrator notes for AI agents

## Environment bootstrap

Always run with the uv-managed venv. The `dev` extras (pytest, ruff, mypy,
types-PyYAML, pytest-asyncio) are **optional** in `pyproject.toml`, so a bare
`uv sync` installs only runtime deps and `uv run pytest` will fail with
`ModuleNotFoundError: No module named 'pytest'`.

Bootstrap with extras:

```bash
uv sync --all-extras
```

Then prefix every command with `uv run` (no manual `source .venv/bin/activate`
needed — `uv run` handles activation):

```bash
uv run pytest -q
uv run factory --help
uv run factory pm-sync --app <app>
uv run factory tick --app <app>
```

If you see `ModuleNotFoundError` for `frontmatter`, `sqlmodel`, or `pytest`,
re-run `uv sync --all-extras` before debugging further — the env is the issue,
not the code.

## Factory Management System (FMS)

The factory monitors and improves itself via a four-tier LLM pipeline:

- **L1 Watcher** (`factory/manager/watcher.py`) — cheap, runs every tick; summarises
  signals from `state/events/*.ndjson` and escalates anomalies to L2.
- **L2 Summarizer** (`factory/manager/summarizer.py`) — mid-tier; writes structured
  concern documents to `state/concerns/`.
- **L3 Diagnostician** (`factory/manager/diagnostician.py`) — frontier-tier; reads a
  concern + relevant source files and produces a unified-diff proposal.
- **L4 Apply** (`factory/manager/apply.py`) — classifies proposals as safe/forbidden,
  creates branches, runs pytest, opens PRs, auto-merges.

**Self-context (Phase 9):** `factory/manager/self_context.py` generates six Markdown
modules under `apps/factory/context/modules/` (orchestrator, personas, state-machine,
observability, dispatch, manager). The L3 Diagnostician loads relevant modules when
building proposals. Refresh via `factory manager refresh-context [--module <name>] [--dry-run]`.

See `apps/factory/directions/001-factory-management-system/direction.md` for the full PRD.
