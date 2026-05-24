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
