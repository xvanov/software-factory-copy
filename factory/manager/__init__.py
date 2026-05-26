"""factory.manager — FMS signal infrastructure (Phase 1).

This package implements the structured event-stream layer that every
higher-level FMS component (L1 Watcher, L2 Summarizer, L3 Diagnostician)
reads. Six append-only NDJSON streams are written under ``state/events/``
by the chain itself; this package owns the writer helpers.

Phase 1 deliverables:
  * ``signals.py`` — shared ``write_event`` helper + per-stream wrappers.
  * The six streams are wired into the orchestrator, runner, and git ops.
  * ``factory manager signals dump`` CLI for operator inspection.
"""
