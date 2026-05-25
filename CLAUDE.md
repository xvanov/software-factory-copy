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
uv run factory pm-sync --app sacrifice
uv run factory tick --app sacrifice
```

If you see `ModuleNotFoundError` for `frontmatter`, `sqlmodel`, or `pytest`,
re-run `uv sync --all-extras` before debugging further — the env is the issue,
not the code.
