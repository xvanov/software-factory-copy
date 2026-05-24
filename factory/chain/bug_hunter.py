"""Bug-Hunter chain — daily security/quality scan.

Thin spec-aligned wrapper around ``run_scheduled_persona`` so the bug-
hunter persona has its own module per the Phase 6 layout. The actual
LLM dispatch, direction filing, rate-limit gating, and DB persistence
all live in ``factory.chain.scheduled_tasks``.

Why split: keeps the public surface for each Phase 6 persona discoverable
(`factory.chain.{ralph,bug_hunter,security,ux_auditor}.<persona>_tick`)
while sharing the dispatch plumbing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from factory.chain.scheduled_tasks import (
    ScheduledRunOutput,
    run_scheduled_persona,
)

# Default cap on directions filed in a single run. The persona's own
# JSON output is the only direction source; this is a defense-in-depth
# guard against a runaway scan flooding the queue.
DEFAULT_MAX_DIRECTIONS = 10


def bug_hunter_tick(
    app: str,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    fixture_output: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> ScheduledRunOutput:
    """Single bug-hunt pass for ``app``.

    Delegates to ``run_scheduled_persona("bug_hunter", ...)`` which:
      * Consults the rate-limit gate (``bug_hunter_runs_per_day``).
      * Invokes the persona (text_run) or returns the fixture (dry-run).
      * Files one direction per finding (capped at the persona's own
        output schema; the chain enforces the rate-limit pre-dispatch).
      * Records a ``scheduled_runs`` row for audit.

    The chain runs ``factory.deploy.runner.run_command`` for any
    scanners the persona claims to have run — but the persona's prompt
    is the actual driver; we don't shell out from here. (Real-run
    scanner invocation lives in a future enhancement; today's bug_hunter
    is JSON-only and trusts the model to report tool output.)
    """
    return run_scheduled_persona(
        "bug_hunter",
        app,
        Path(software_factory_root),
        dry_run=dry_run,
        fixture_output=fixture_output,
        db_path=db_path,
    )


__all__ = ["DEFAULT_MAX_DIRECTIONS", "bug_hunter_tick"]
