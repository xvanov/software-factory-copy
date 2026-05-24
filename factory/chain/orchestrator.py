"""Chain orchestrator — drive in-flight stories one tick at a time.

The orchestrator inspects every non-terminal StoryRecord for an app and
invokes the appropriate handler for its current state. One ``tick()`` call
advances each story by ONE handler (not the full chain). Phase 3+ will hook
``tick()`` to webhook events; for Phase 2 it's manual via ``factory tick``.

Phase 3 wires the **settings enforcer** in front of every handler dispatch.
``can_dispatch`` reads the current factory mode + caps from
``factory_settings.yaml`` and the local state.db, and may reject a job with
a structured ``rejected_reason`` (e.g. ``daily_spend_cap_exceeded``). When
rejected, the orchestrator records the reason on the StoryRecord and skips
the story for this tick; an operator can inspect via ``factory why``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlmodel import Session, create_engine, select

from factory.app_config import AppConfig, load_app_config
from factory.chain import handlers as H
from factory.chain.state_machine import StoryRecord, StoryState
from factory.directions.parser import Direction
from factory.settings.enforcer import can_dispatch
from factory.settings.loader import load_settings
from factory.settings.modes import get_mode
from factory.settings.spend import hour_spend_usd, today_spend_usd

# Handler kinds that have a "bug-fix variant" recognized by the enforcer's
# ``fix-only`` mode. Kept in sync with
# ``factory/settings/enforcer.py:_BUG_FIX_JOB_KINDS``.
_BUG_AWARE_HANDLER_KINDS = {"sm", "test_design", "test_impl", "dev", "review"}


def _resolve_job_kind(
    story: StoryRecord,
    direction: Direction | None,
    handler_kind: str,
) -> str:
    """Compute the ``job_kind`` to pass to ``can_dispatch``.

    Bug-typed work (``direction.type_tag == "bug"`` or ``story.scope ==
    "bug"``) is appended with a ``-bug`` suffix so the enforcer's
    ``fix-only`` mode permits the dispatch while still blocking feature
    work. The enforcer's ``_mode_blocks`` already understands the suffix;
    this helper is the single producer of the suffixed kinds.

    Returns ``handler_kind`` unchanged for kinds that have no bug variant.
    """
    if handler_kind not in _BUG_AWARE_HANDLER_KINDS:
        return handler_kind
    type_tag = (direction.type_tag if direction is not None else None) or ""
    is_bug = type_tag.lower() == "bug" or story.scope == "bug"
    return f"{handler_kind}-bug" if is_bug else handler_kind


@dataclass
class TickSummary:
    """What happened in this tick."""

    app: str
    dry_run: bool
    stories_advanced: int = 0
    blocked_by_caps: int = 0  # rejected by can_dispatch (mode, cap, rate-limit)
    stories_blocked: int = 0  # blocked mid-chain (BLOCKED state)
    handler_runs: list[tuple[str, str, str]] = field(default_factory=list)
    # ^ (story_slug, from_state, to_state)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    # ^ (story_slug, rejected_reason)
    errors: list[tuple[str, str]] = field(default_factory=list)


# Per-state handler dispatch — what to run when a story is in this state.
# Returns the (handler-callable, kwargs-builder). The orchestrator calls the
# handler; the kwargs-builder is responsible for translating per-state
# pre-conditions (e.g. dev_in_progress -> the dev handler is the one that
# transitioned into dev_in_progress, so we don't re-run; only DEV_RETRY and
# the entry states get a handler).
_DISPATCH = {
    StoryState.STORY_CREATED: "sm",
    StoryState.SM_DONE: "test_design",
    # TODO(phase-3-or-4): Insert "architect" before test_design when the PM's
    # ``child_stories`` count crosses the architectural threshold or the
    # story scope is ``infra``. See ``factory/personas/architect.md`` for the
    # prompt; the handler should rewrite ``context/current-state.md`` BEFORE
    # the Test-Designer reads it. SM's prompt already documents the
    # threshold (3+ stories, ``infra`` scope, schema/migration/dependency in
    # the title), so the orchestrator can read sm_result_json to decide.
    StoryState.TEST_DESIGN_DONE: "test_impl",
    StoryState.TESTS_RED: "dev",
    StoryState.DEV_RETRY: "dev",
    StoryState.TESTS_GREEN: "review",
    # TODO(phase-3-or-4): Invoke ``ux_designer`` (see
    # ``factory/personas/ux_designer.md``) from inside the SM handler when
    # the direction has UI scope and flow.md ambiguity is detected (no
    # explicit user-visible steps, or contradictory descriptions). For now
    # SM produces stories as-is; the ux_designer persona file is wired but
    # not dispatched.
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
    if name == "sm":
        return H.handle_sm(
            story, app_config, software_factory_root, dry_run=dry_run, db_path=db_path
        )
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


def _count_global_in_flight(db: Path, exclude_story_id: int | None = None) -> int:
    """Count non-terminal stories across all apps (for the global cap).

    ``exclude_story_id`` lets the orchestrator subtract the story it's
    currently inspecting so the cap measures "competitors", not "myself".
    """
    eng = create_engine(f"sqlite:///{db}", echo=False)
    terminal = {
        StoryState.PR_OPEN.value,
        StoryState.CI_PENDING.value,
        StoryState.CI_GREEN.value,
        StoryState.READY_FOR_MERGE.value,
        StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value,
    }
    with Session(eng) as session:
        rows = session.exec(select(StoryRecord)).all()
    return sum(
        1
        for r in rows
        if r.state not in terminal and (exclude_story_id is None or r.id != exclude_story_id)
    )


def _build_current_state(
    *,
    root: Path,
    db: Path,
    app: str,
    in_flight_app: int,
    exclude_story_id: int | None = None,
) -> dict[str, Any]:
    """Compose the dict passed to ``can_dispatch``.

    Spends are computed from the local ``runs`` table; PR counts and CI red
    counts are intentionally left ``None`` (unknown) when we can't reach
    GitHub. The enforcer treats ``None`` as "skip this check" so the dry-run
    path doesn't make network calls.
    """
    return {
        "mode": get_mode(root, db_path=db),
        "global_in_flight": _count_global_in_flight(db, exclude_story_id=exclude_story_id),
        "app_in_flight": in_flight_app,
        "today_spend_usd": today_spend_usd(root, db_path=db),
        "hour_spend_usd": hour_spend_usd(root, db_path=db),
        "open_prs_for_app": None,  # filled in by webhook-driven path
        "failing_ci_count": None,  # filled in by webhook-driven path
        "pm_invocations_last_hour": 0,
    }


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

    Before each handler the settings enforcer is consulted. If a job is
    rejected, the story's ``last_rejection_reason`` is set and the
    orchestrator moves on to the next story.
    """
    root = Path(software_factory_root)
    db = db_path or (root / "state" / "factory.db")

    try:
        cfg = load_app_config(app, root)
    except FileNotFoundError as exc:
        return TickSummary(app=app, dry_run=dry_run, errors=[(app, f"app config missing: {exc}")])

    settings = load_settings(root)
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

            # Backpressure check before dispatch. The current_state dict is
            # recomputed each iteration so the in-flight counts reflect any
            # newly-completed stories. The current story is part of those
            # counts; subtract 1 so the cap measures "stories blocked by
            # this dispatch in addition to me", not "would I be the N+1th".
            in_flight_app = max(0, len(H.stories_in_flight(app, db)) - 1)
            state_dict = _build_current_state(
                root=root,
                db=db,
                app=app,
                in_flight_app=in_flight_app,
                exclude_story_id=story.id,
            )
            # Resolve the actual job_kind to dispatch — bug-typed directions
            # get a "-bug" suffix so ``fix-only`` mode lets the work
            # proceed while still blocking feature stories.
            direction = H.find_direction_for_story(story, root)
            job_kind = _resolve_job_kind(story, direction, handler_name)
            decision = can_dispatch(job_kind, app, state_dict, settings)
            if not decision.allowed:
                story.last_rejection_reason = decision.rejected_reason
                H.persist_story(story, db)
                summary.blocked_by_caps += 1
                summary.rejected.append((story.slug, decision.rejected_reason or "unknown"))
                break
            # Job is allowed — clear any stale rejection reason.
            if story.last_rejection_reason is not None:
                story.last_rejection_reason = None
                H.persist_story(story, db)

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
        "blocked_by_caps": summary.blocked_by_caps,
        "stories_blocked": summary.stories_blocked,
        "handler_runs": summary.handler_runs,
        "rejected": summary.rejected,
        "errors": summary.errors,
    }
