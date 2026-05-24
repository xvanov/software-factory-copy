"""Chain orchestrator — drive in-flight stories one tick at a time.

The orchestrator inspects every non-terminal StoryRecord for an app and
invokes the appropriate handler for its current state. One ``tick()`` call
advances each story by ONE handler (not the full chain). Phase 3+ will hook
``tick()`` to webhook events; for Phase 2 it's manual via ``factory tick``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.app_config import AppConfig, load_app_config
from factory.chain import handlers as H
from factory.chain.state_machine import StoryRecord, StoryState


@dataclass
class TickSummary:
    """What happened in this tick."""

    app: str
    dry_run: bool
    stories_advanced: int = 0
    stories_blocked: int = 0
    handler_runs: list[tuple[str, str, str]] = field(default_factory=list)
    # ^ (story_slug, from_state, to_state)
    errors: list[tuple[str, str]] = field(default_factory=list)


# Per-state handler dispatch — what to run when a story is in this state.
# Returns the (handler-callable, kwargs-builder). The orchestrator calls the
# handler; the kwargs-builder is responsible for translating per-state
# pre-conditions (e.g. dev_in_progress -> the dev handler is the one that
# transitioned into dev_in_progress, so we don't re-run; only DEV_RETRY and
# the entry states get a handler).
_DISPATCH = {
    StoryState.STORY_CREATED: "test_design",
    StoryState.TEST_DESIGN_DONE: "test_impl",
    StoryState.TESTS_RED: "dev",
    StoryState.DEV_RETRY: "dev",
    StoryState.TESTS_GREEN: "review",
    StoryState.REVIEWER_DONE: "tech_writer",
    StoryState.TECH_WRITER_DONE: "docs_enforcer",
}


def _invoke_handler(
    name: str,
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool,
    db_path: Path,
) -> H.HandlerResult:
    if name == "test_design":
        return H.handle_test_design(
            story, app_config, software_factory_root, dry_run=dry_run, db_path=db_path
        )
    if name == "test_impl":
        return H.handle_test_implementation(
            story, app_config, software_factory_root, dry_run=dry_run, db_path=db_path
        )
    if name == "dev":
        return H.handle_dev(
            story, app_config, software_factory_root, dry_run=dry_run, db_path=db_path
        )
    if name == "review":
        return H.handle_review(
            story, app_config, software_factory_root, dry_run=dry_run, db_path=db_path
        )
    if name == "tech_writer":
        return H.handle_tech_writer(
            story, app_config, software_factory_root, dry_run=dry_run, db_path=db_path
        )
    if name == "docs_enforcer":
        return H.handle_docs_enforcer(
            story, app_config, software_factory_root, dry_run=dry_run, db_path=db_path
        )
    raise RuntimeError(f"unknown handler name: {name}")


def tick(
    software_factory_root: Path,
    app: str,
    *,
    dry_run: bool = False,
    max_advances_per_story: int = 10,
    db_path: Path | None = None,
) -> TickSummary:
    """Advance every in-flight story for ``app`` toward PR_OPEN.

    Each story is driven through as many handlers as possible (up to
    ``max_advances_per_story``) so the dry-run dogfood completes in a single
    tick call. Real-run will typically only advance one handler per tick
    because webhooks gate progress (CI green, PR open, etc).
    """
    root = Path(software_factory_root)
    db = db_path or (root / "state" / "factory.db")

    try:
        cfg = load_app_config(app, root)
    except FileNotFoundError as exc:
        return TickSummary(app=app, dry_run=dry_run, errors=[(app, f"app config missing: {exc}")])

    summary = TickSummary(app=app, dry_run=dry_run)
    stories = H.stories_in_flight(app, db)

    if not stories:
        return summary

    for story in stories:
        # Advance up to ``max_advances_per_story`` steps for this story.
        for _ in range(max_advances_per_story):
            current = StoryState(story.state)
            handler_name = _DISPATCH.get(current)
            if handler_name is None:
                # No handler for this state — either in-progress (waiting on
                # webhook) or terminal. Stop driving.
                break
            from_state = story.state
            try:
                result = _invoke_handler(
                    handler_name,
                    story,
                    cfg,
                    root,
                    dry_run=dry_run,
                    db_path=db,
                )
            except Exception as exc:
                summary.errors.append((story.slug, repr(exc)))
                break
            summary.handler_runs.append((story.slug, from_state, story.state))
            summary.stories_advanced += 1
            if result.error or story.state == StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value:
                summary.stories_blocked += 1
                break
            if story.state == StoryState.PR_OPEN.value:
                break

    return summary


def tick_summary_as_dict(summary: TickSummary) -> dict[str, Any]:
    return {
        "app": summary.app,
        "dry_run": summary.dry_run,
        "stories_advanced": summary.stories_advanced,
        "stories_blocked": summary.stories_blocked,
        "handler_runs": summary.handler_runs,
        "errors": summary.errors,
    }
