# AGENTS.md

Entry point for AGENTS.md-aware tools (OpenCode, Codex, Cursor, …) working in
this repo.

**Read `CLAUDE.md` first — it is the full brief and it is short.** It covers:
what this factory is, the three loops and which one you are in, the 60-second
health check, the environment (`uv sync --all-extras`, then prefix everything
with `uv run`), where truth lives, the operator command surface, the
diagnose→fix→deploy playbook, the hard guardrails, and pointers to deeper docs.

Do not start editing before you have read it.

## The five rules you cannot violate

1. `uv sync --all-extras`, then `uv run <cmd>`. A bare `uv sync` has no pytest.
2. The live tree must equal `origin/main` — `git fetch origin && git status -sb`.
   It has silently run dozens of commits behind before.
3. Never `git add -A` in this tree (`state/**` is live runtime churn). Deploy
   with `scripts/deploy-factory-from-main.sh`.
4. `factory/manager/**` and `bench/**` are forbidden to self-edit (operator PR
   only). Every self-edit merge surface stays staging-gated.
5. Gate on the real artifact, never a proxy (a recorded flag, an `--auto`
   *enable*, a dry-run's intent, a green test run with no commit). Fail safe.

## Working in an app repo instead?

Each app has its own agent docs — `../sacrifice/CLAUDE.md`,
`../rental-management/AGENTS.md`, `../template/CLAUDE.md`. This file is only
about the orchestrator.

## Testing gotcha (story D012 follow-up)

- Slop detector rule `direct_db_bootstrap` flags tests that call `create_engine(...)`/`SQLModel.metadata.create_all(...)` directly. For DB assertions in tests, bootstrap through `factory.observability.schema.migrate(db_path)` and query with `sqlite3` instead of creating engines inside the test body.
