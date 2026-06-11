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

import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, create_engine, select

from factory.app_config import AppConfig, load_app_config
from factory.chain import handlers as H
from factory.chain.auto_merge import MergeAction, auto_merge_tick
from factory.chain.event_log import log_story_event
from factory.chain.state_machine import StoryRecord, StoryState
from factory.directions.parser import Direction
from factory.settings.enforcer import can_dispatch
from factory.settings.loader import load_settings
from factory.settings.modes import get_mode
from factory.settings.spend import hour_spend_usd, today_spend_usd

# Handler kinds that have a "bug-fix variant" recognized by the enforcer's
# ``fix-only`` mode. Kept in sync with
# ``factory/settings/enforcer.py:_BUG_FIX_JOB_KINDS``.
_BUG_AWARE_HANDLER_KINDS = {"sm", "dev", "review"}


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
    # End-of-tick auto-merge decisions (one entry per PR evaluated).
    # Empty when ``auto_merge.enabled=false`` or no PRs are eligible.
    merges: list[MergeAction] = field(default_factory=list)
    # Phase 7: set to True when tick exits early due to factory halt.
    halted: bool = False
    halt_reason: str | None = None


# Per-state handler dispatch — what to run when a story is in this state.
# Returns the (handler-callable, kwargs-builder). The orchestrator calls the
# handler; the kwargs-builder is responsible for translating per-state
# pre-conditions (e.g. dev_in_progress -> the dev handler is the one that
# transitioned into dev_in_progress, so we don't re-run; only DEV_RETRY and
# the entry states get a handler).
# Per-state handler dispatch — what to run when a story is in this state.
#
# STORY_CREATED is special: which handler runs depends on the story's
# ``chain_kind``. The actual dispatch decision lives in ``_dispatch_for_story``
# so the state machine stays pure. Stories with chain_kind="docs" route to
# ``docs_sm`` here; chain_kind="tdd" (default) routes to ``sm``.
_DISPATCH = {
    StoryState.STORY_CREATED: "sm",  # TDD default; overridden for docs chain
    # Loop-4 (dev-owns-tests): SM_DONE dispatches dev DIRECTLY. The dev persona
    # writes production code + its tests in one pass; there is no separate
    # test_design/test_impl/harness phase.
    StoryState.SM_DONE: "dev",
    StoryState.DEV_RETRY: "dev",
    # When the reviewer pushes back to REVIEWER_REQUESTED_CHANGES, the
    # state machine routes dev_started → DEV_IN_PROGRESS but the
    # dispatcher had no entry, leaving the story stuck. Same handler as
    # the dev-retry path.
    StoryState.REVIEWER_REQUESTED_CHANGES: "dev",
    StoryState.TESTS_GREEN: "review",
    # TODO(phase-3-or-4): Invoke ``ux_designer`` (see
    # ``factory/personas/ux_designer.md``) from inside the SM handler when
    # the direction has UI scope and flow.md ambiguity is detected (no
    # explicit user-visible steps, or contradictory descriptions). For now
    # SM produces stories as-is; the ux_designer persona file is wired but
    # not dispatched.
    StoryState.REVIEWER_DONE: "tech_writer",
    StoryState.TECH_WRITER_DONE: "docs_enforcer",
    # Docs chain dispatch (skips the TDD red→green loop).
    StoryState.DOCS_SM_DONE: "docs_onboarder",
    StoryState.DOCS_ONBOARDER_DONE: "docs_enforcer",
    # Phase 5 — post-merge deploy. The auto-merge worker (and the webhook
    # path) flips a story to DEPLOY_PENDING; from there the orchestrator
    # tick drives handle_deploy.
    StoryState.DEPLOY_PENDING: "deploy",
}


def _dispatch_for_story(story: StoryRecord) -> str | None:
    """Pick the handler name for ``story`` given its current state.

    Pure wrapper around ``_DISPATCH`` plus one branch: ``STORY_CREATED``
    depends on ``story.chain_kind`` (tdd → sm, docs → docs_sm).
    """
    state = StoryState(story.state)
    if state == StoryState.STORY_CREATED:
        if story.chain_kind == "docs":
            return "docs_sm"
        return "sm"
    return _DISPATCH.get(state)


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
    if name == "docs_sm":
        return H.handle_docs_sm(
            story, app_config, software_factory_root, dry_run=dry_run, db_path=db_path
        )
    if name == "docs_onboarder":
        return H.handle_docs_onboarder(
            story, app_config, software_factory_root, dry_run=dry_run, db_path=db_path
        )
    if name == "deploy":
        return H.handle_deploy(
            story, app_config, software_factory_root, dry_run=dry_run, db_path=db_path
        )
    raise RuntimeError(f"unknown handler name: {name}")


# States that don't count toward concurrency caps. ``STORY_CREATED`` is a
# pre-dispatch queue state — no agent is running yet, the story is just
# waiting for its first handler. PM-sync routinely spawns N children at
# once into STORY_CREATED; without this exclusion the cap deadlocks the
# whole batch (every story sees the other N-1 as competitors and is
# refused, so none ever advances). Terminal states are excluded for the
# usual reason: the work is done.
_NON_CAP_COUNTING_STATES = {
    # pre-dispatch
    StoryState.STORY_CREATED.value,
    # terminal / post-orchestrator
    StoryState.PR_OPEN.value,
    StoryState.CI_PENDING.value,
    StoryState.CI_GREEN.value,
    StoryState.READY_FOR_MERGE.value,
    StoryState.DEPLOYED.value,
    StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value,
    StoryState.BLOCKED_DEPLOY_FAILED.value,
    StoryState.BLOCKED_REVIEW_NONCONVERGENT.value,
    # Passive transition states — no agent is actively running; the story
    # is simply waiting for the orchestrator to dispatch the next handler
    # on the next tick. Counting these against the cap deadlocks any
    # operator-reset batch (e.g. 31 stories moved out of
    # blocked_tests_need_clarification back to tests_red), and more
    # generally inflates the "in-flight" count beyond the number of
    # actually-running agents. The cap exists to limit concurrent agents;
    # idle queue states should not consume slots. Same rationale as
    # STORY_CREATED above (PM-sync spawning N children at once).
    StoryState.SM_DONE.value,
    StoryState.TESTS_GREEN.value,
    StoryState.DEV_RETRY.value,
    StoryState.REVIEWER_DONE.value,
    StoryState.REVIEWER_REQUESTED_CHANGES.value,
    StoryState.DOCS_ONBOARDER_DONE.value,
    StoryState.DEPLOY_PENDING.value,
}


def _count_global_in_flight(db: Path, exclude_story_id: int | None = None) -> int:
    """Count actively-dispatched stories across all apps (for the global cap).

    ``exclude_story_id`` lets the orchestrator subtract the story it's
    currently inspecting so the cap measures "competitors", not "myself".

    Stories in ``STORY_CREATED`` are queued (no agent dispatched yet) and
    therefore do not count — see ``_NON_CAP_COUNTING_STATES``.
    """
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        rows = session.exec(select(StoryRecord)).all()
    return sum(
        1
        for r in rows
        if r.state not in _NON_CAP_COUNTING_STATES
        and (exclude_story_id is None or r.id != exclude_story_id)
    )


def _count_app_in_flight(db: Path, app: str, exclude_story_id: int | None = None) -> int:
    """Count actively-dispatched stories for ``app`` (for the per-repo cap).

    Same queued-vs-dispatched semantics as ``_count_global_in_flight``.
    """
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        rows = session.exec(select(StoryRecord).where(StoryRecord.app == app)).all()
    return sum(
        1
        for r in rows
        if r.state not in _NON_CAP_COUNTING_STATES
        and (exclude_story_id is None or r.id != exclude_story_id)
    )


# Docs-chain serialization (loop-3 fix). Multiple docs stories for the same
# app rewrite an overlapping set of canonical ``context/*.md`` files
# (current-state.md, project.md, navigation.md, the shared module docs…). When
# two docs PRs are open at once, whichever auto-merges second goes DIRTY /
# CONFLICTING and dies in ``blocked_deploy_failed`` (observed: PRs #88/#89).
# The agent-concurrency cap can't prevent this because PR_OPEN and the CI/merge
# states are in ``_NON_CAP_COUNTING_STATES`` — two docs stories can both sit in
# PR_OPEN and conflict at merge time. So we serialize docs stories with a
# dedicated counter that DOES span the open-PR/merge window: a docs story is
# "active" from the moment its first handler is dispatched until it is DEPLOYED
# or terminal. While any docs story for an app is active, no *other* docs story
# for that app leaves STORY_CREATED — the next one waits, then regenerates its
# diff against the prior story's already-merged content (conflict-free).
_DOCS_ACTIVE_STATES = {
    StoryState.DOCS_SM_IN_PROGRESS.value,
    StoryState.DOCS_SM_DONE.value,
    StoryState.DOCS_ONBOARDER_IN_PROGRESS.value,
    StoryState.DOCS_ONBOARDER_DONE.value,
    StoryState.DOCS_ENFORCER_CHECK.value,
    StoryState.PR_OPEN.value,
    StoryState.CI_PENDING.value,
    StoryState.CI_GREEN.value,
    StoryState.READY_FOR_MERGE.value,
    StoryState.DEPLOY_PENDING.value,
}


def _count_app_docs_active(db: Path, app: str, exclude_story_id: int | None = None) -> int:
    """Count docs-chain stories for ``app`` with a live (or pending) PR.

    Spans the whole open-PR/merge window (see ``_DOCS_ACTIVE_STATES``) so the
    serialization gate holds two docs PRs from being open simultaneously.
    """
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        rows = session.exec(select(StoryRecord).where(StoryRecord.app == app)).all()
    return sum(
        1
        for r in rows
        if r.chain_kind == "docs"
        and r.state in _DOCS_ACTIVE_STATES
        and (exclude_story_id is None or r.id != exclude_story_id)
    )


def _direction_deps_pending(db: Path, story: StoryRecord) -> list[int]:
    """Return lower-id stories in the same direction that are NOT yet deployed.

    Dependency ordering. Within a direction the SM emits stories
    foundational-first (model → service → endpoint → smoke → UI → docs), so the
    story-id order IS the build/dependency order. A dependent story (e.g. a
    smoke test, or a UI story) cannot build correctly until the foundations it
    relies on are merged to main — building it early surfaces "endpoint missing
    / test failing / no migration" defects that are really just out-of-order
    construction (the root cause that stranded the interdependent D008-D010
    batch). A story is dependency-ready only when every lower-id story in its
    own direction has reached ``deployed``. Empty list == ready.

    Pure read; no schema change. Cross-direction ordering is intentionally NOT
    enforced here (directions are treated as independent features).
    """
    if story.id is None:
        return []
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        siblings = session.exec(
            select(StoryRecord).where(
                StoryRecord.app == story.app,
                StoryRecord.direction_id == story.direction_id,
            )
        ).all()
    return sorted(
        s.id
        for s in siblings
        if s.id is not None
        and s.id < story.id
        and s.state != StoryState.DEPLOYED.value
    )


# Mapping from a stranded ``*_in_progress`` state back to its
# dispatch-eligible predecessor. Used by ``_prune_stale_in_progress`` to
# recover rows that didn't reach the handler's normal exit (process kill,
# uncaught exception, dirty-tree race, retry-cap change mid-attempt).
_STALE_RECOVERY_MAP: dict[str, str] = {
    "sm_in_progress": "story_created",
    "dev_in_progress": "dev_retry",
    "reviewer_in_progress": "tests_green",
    "tech_writer_in_progress": "reviewer_done",
    "docs_sm_in_progress": "story_created",
    "docs_onboarder_in_progress": "docs_sm_done",
}


# How long an ``*_in_progress`` row can sit without a row-level update
# before the cleanup pass considers it stranded. Picked to be just above
# the worst-case legitimate sandbox run (dev iteration cap is 600 calls
# ≈ 8–12 min in real LLM timing) so we recover stuck rows quickly
# without racing a live in-progress sandbox. Operators wanting more
# aggressive recovery can lower this and accept the false-positive risk.
_STALE_THRESHOLD_SECONDS = 10 * 60


def _prune_stale_in_progress(
    db: Path,
    app: str,
    *,
    settings: Any,
    root: Path,
    now: datetime | None = None,
) -> list[tuple[str, str, str]]:
    """Recover stories stranded in ``*_in_progress`` from a crashed tick.

    A ``StoryRecord`` rolls into an ``_in_progress`` state when a handler
    starts; the handler exits normally by transitioning OUT of that state.
    Several failure modes can break that contract:

      * The tick process is killed mid-sandbox (SIGTERM, OOM).
      * A subtle inner-loop bug calls a handler twice and the second call
        raises ``IllegalTransitionError`` before the first has transitioned
        out.
      * ``_MAX_DEV_RETRIES`` (or another guarded threshold) is lowered
        between ticks while a row is mid-attempt — the in-flight handler
        completes under the old regime but the row's state is invalidated
        for the new regime.

    Once stranded, ``_dispatch_for_story`` returns ``None`` for
    ``*_in_progress`` (those slots are webhook-driven), so the chain has
    no way to nudge the row forward without operator intervention.

    This pass detects rows older than ``_STALE_THRESHOLD_SECONDS`` and
    rolls them back to the most-recent dispatch-eligible state per
    ``_STALE_RECOVERY_MAP``. For ``dev_in_progress`` we ALSO clamp
    ``dev_retries`` to ``MAX_DEV_RETRIES - 1`` so the next dispatch gives
    the story exactly one fresh attempt and then exhausts naturally —
    without that clamp, a row stranded under the old cap=10 regime would
    immediately exhaust on its first new dispatch under cap=3, which is
    surprising and wastes the diagnostic the operator might want from a
    single observed-under-new-cap run.

    Emits one ``stale_recovery`` event per recovered story to the
    per-story log so operators can see what was nudged and why. Returns
    the list of (slug, from_state, to_state) tuples; the orchestrator
    surfaces these in ``TickSummary.handler_runs`` as
    ``"<from_state>(stale)"``.
    """
    from datetime import UTC
    from datetime import datetime as _dt

    from factory.chain.event_log import log_story_event
    from factory.chain.handlers import _MAX_DEV_RETRIES, persist_story

    now_ts = (now or _dt.now(UTC)).timestamp()
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        candidates = session.exec(select(StoryRecord).where(StoryRecord.app == app)).all()

    recovered: list[tuple[str, str, str]] = []
    for story in candidates:
        target = _STALE_RECOVERY_MAP.get(story.state)
        if target is None:
            continue
        try:
            updated_iso = story.updated_at or story.created_at
            updated_ts = _dt.fromisoformat(updated_iso).timestamp()
        except (TypeError, ValueError):
            updated_ts = 0  # treat unparseable timestamps as ancient
        if (now_ts - updated_ts) < _STALE_THRESHOLD_SECONDS:
            continue

        from_state = story.state
        story.state = target
        # Clamp dev_retries so the recovered row gets one fresh shot
        # under whatever the current cap is, instead of insta-exhausting
        # on a stale count from a previous cap regime.
        if from_state == "dev_in_progress" and story.dev_retries >= _MAX_DEV_RETRIES:
            story.dev_retries = max(0, _MAX_DEV_RETRIES - 1)
        story.error = (
            f"stale-state recovery: rolled back from {from_state!r} "
            f"(no row update for >{_STALE_THRESHOLD_SECONDS // 60} min)"
        )
        persist_story(story, db)
        recovered.append((story.slug, from_state, target))
        log_story_event(
            story.id,
            "stale_recovery",
            {
                "from_state": from_state,
                "to_state": target,
                "dev_retries_after_clamp": story.dev_retries,
                "age_seconds": int(now_ts - updated_ts),
            },
            software_factory_root=root,
            slug_hint=story.slug,
        )

    return recovered


# A blocked story is a factory defect, never a real outcome (operator rule:
# "there can only be 0 blocked stories"). When the orchestrator's chain code is
# fixed, stories already sitting in a terminal blocked state from the *old*
# (now-fixed) regime stay blocked forever — there is no transition out of a
# blocked state, so the only escape used to be a manual DB reset. This pass
# closes that gap: it re-dispatches blocked stories back into the chain so a
# since-shipped fix actually reaches them. Bounded per story so a genuinely
# unsatisfiable story (contradictory contract, etc.) surfaces to a human after
# a couple of honest re-attempts instead of being recycled forever.
_MAX_AUTO_RECOVERIES = 2
# Blocked states recovered by re-entering the chain at SM_DONE (re-runs
# dev -> review -> merge with current fixes; the dev writes code + tests).
# deploy_failed is intentionally excluded — it's handled at the merge layer by
# ``auto_merge._attempt_pr_reconcile`` (stale branches) and true content
# conflicts there need regeneration/human handling, not a dev re-run.
_AUTO_RECOVERABLE_STATES: dict[str, str] = {
    StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value: StoryState.SM_DONE.value,
    StoryState.BLOCKED_REVIEW_NONCONVERGENT.value: StoryState.SM_DONE.value,
}


def _recover_blocked_stories(
    db: Path, app: str, *, root: Path
) -> list[tuple[str, str, str]]:
    """Re-dispatch blocked stories so since-shipped chain fixes reach them.

    For each story in an auto-recoverable blocked state, reset it to the
    re-entry point (SM_DONE → dev) with a clean slate (retry/cycle counters
    cleared, stale reviewer payload cleared) so it flows through the current
    chain from scratch. Bounded to ``_MAX_AUTO_RECOVERIES``
    per story via ``auto_recovery`` events in the per-story log; once exhausted
    the story stays blocked and an ``auto_recovery_exhausted`` /
    ``factory_needs_redesign`` event fires so the FMS/operator sees a genuinely
    stuck story rather than an endless recycle.

    Pure DB rewrite — no LLM/git work — mirroring ``_prune_stale_in_progress``.
    Returns (slug, from_state, to_state) tuples for the TickSummary.
    """
    from factory.chain.event_log import log_story_event, read_story_events
    from factory.chain.handlers import persist_story

    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        candidates = session.exec(select(StoryRecord).where(StoryRecord.app == app)).all()

    recovered: list[tuple[str, str, str]] = []
    for story in candidates:
        target = _AUTO_RECOVERABLE_STATES.get(story.state)
        if target is None:
            continue
        # Only recoveries into the CURRENT re-entry point consume the budget.
        # When the chain is redesigned the re-entry target changes (e.g. the
        # old test-first regime re-entered at tests_red; Loop-4 re-enters at
        # sm_done), and attempts burnt under the old regime say nothing about
        # whether the new chain can converge the story — the budget resets.
        prior = sum(
            1
            for e in read_story_events(
                story.id, software_factory_root=root, slug_hint=story.slug
            )
            if e.get("event") == "auto_recovery" and e.get("to_state") == target
        )
        if prior >= _MAX_AUTO_RECOVERIES:
            # Already re-attempted the allowed number of times and still
            # blocked → genuinely stuck. Emit a loud, deduped escalation once.
            already_escalated = any(
                e.get("event") == "auto_recovery_exhausted"
                for e in read_story_events(
                    story.id, software_factory_root=root, slug_hint=story.slug
                )
            )
            if not already_escalated:
                log_story_event(
                    story.id,
                    "auto_recovery_exhausted",
                    {
                        "state": story.state,
                        "recoveries": prior,
                        "error": (story.error or "")[:300],
                    },
                    software_factory_root=root,
                    slug_hint=story.slug,
                )
            continue

        from_state = story.state
        story.state = target
        story.error = None
        story.dev_retries = 0
        story.reviewer_cycles = 0
        story.reviewer_result_json = None
        story.last_rejection_reason = None
        story.current_model_tier = "standard"
        story.harness_precheck_passed = False
        persist_story(story, db)
        recovered.append((story.slug, from_state, target))
        log_story_event(
            story.id,
            "auto_recovery",
            {
                "from_state": from_state,
                "to_state": target,
                "attempt": prior + 1,
                "cap": _MAX_AUTO_RECOVERIES,
            },
            software_factory_root=root,
            slug_hint=story.slug,
        )

    return recovered


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

    # Dry-run isolation: when the caller did not pass an explicit ``db_path``
    # (i.e. the CLI is hitting the production state DB), copy the live DB to
    # a temp file so dry-run handler state mutations (advance() +
    # persist_story) don't pollute the real DB. Without this, a single
    # dry-run tick can advance a story all the way to ``pr_open`` in the
    # real DB, causing the next real tick to treat the story as terminal
    # and skip it. Tests that pass an explicit ``db_path`` opt out of
    # isolation — they own the DB they hand in.
    _dry_run_db_temp: Path | None = None
    if dry_run and db_path is None and db.exists():
        import shutil
        import tempfile

        fd, tmp_name = tempfile.mkstemp(prefix="factory_dryrun_", suffix=".db")
        os.close(fd)
        _dry_run_db_temp = Path(tmp_name)
        shutil.copyfile(db, _dry_run_db_temp)
        db = _dry_run_db_temp

    # Unique ID for this tick invocation; threaded into runs.ndjson so
    # each run record can be linked back to the tick that spawned it.
    tick_id = str(uuid.uuid4())
    _tick_t0 = datetime.now(UTC).timestamp()

    try:
        cfg = load_app_config(app, root)
    except FileNotFoundError as exc:
        if _dry_run_db_temp is not None:
            _dry_run_db_temp.unlink(missing_ok=True)
        return TickSummary(app=app, dry_run=dry_run, errors=[(app, f"app config missing: {exc}")])

    # Phase 7 — halt check (double defence: also enforced in drive_chain.sh).
    # If the L3 Diagnostician has written a halt state file, skip all dispatch
    # and return a halted TickSummary so even direct ``factory tick`` calls
    # honour the halt without burning LLM credits.
    try:
        from factory.manager.halt import get_halt_state, is_halted

        if is_halted(root=root):
            halt_state = get_halt_state(root=root) or {}
            _halt_reason = halt_state.get("reason", "unknown")
            if _dry_run_db_temp is not None:
                _dry_run_db_temp.unlink(missing_ok=True)
            return TickSummary(
                app=app,
                dry_run=dry_run,
                halted=True,
                halt_reason=_halt_reason,
            )
    except Exception as _halt_exc:  # noqa: BLE001
        # Phase 8 (Phase 7 reviewer note): log the exception to stderr so an
        # operator notices the broken halt module.  Continue with halt=False
        # (fail-open: a broken halt module must not silently prevent all ticks).
        import sys as _sys
        print(
            f"[orchestrator] WARNING: halt-check raised an exception: {_halt_exc!r}; "
            "continuing with tick (fail-open). This may indicate a broken halt module.",
            file=_sys.stderr,
        )

    settings = load_settings(root)
    summary = TickSummary(app=app, dry_run=dry_run)

    # ---- Signal: tick_start ------------------------------------------------
    try:
        from factory.manager.signals import write_tick_event

        write_tick_event(
            "tick_start",
            tick_id=tick_id,
            app=app,
            dry_run=dry_run,
            software_factory_root=root,
        )
    except Exception:  # noqa: BLE001
        pass

    # Track outcome for the guaranteed tick_end signal in the finally block.
    _tick_succeeded = False
    _tick_exception: str | None = None

    try:
        # Recover stories stranded in ``*_in_progress`` from a crashed tick or
        # a prior config-change regime (e.g. retry-cap lowered while a row was
        # mid-attempt). Pure DB rewrite — no LLM / git work. See the function
        # docstring for the recovery mapping.
        if not dry_run:
            try:
                recovered = _prune_stale_in_progress(db, app, settings=settings, root=root)
                for slug, from_state, to_state in recovered:
                    summary.handler_runs.append((slug, f"{from_state}(stale)", to_state))
            except Exception as exc:
                summary.errors.append((app, f"stale-state recovery failed (non-fatal): {exc!r}"))

            # A blocked story is a factory defect — re-dispatch blocked stories
            # so since-shipped chain fixes reach them, instead of requiring a
            # manual DB reset. Bounded per story (see _recover_blocked_stories).
            try:
                re_dispatched = _recover_blocked_stories(db, app, root=root)
                for slug, from_state, to_state in re_dispatched:
                    summary.handler_runs.append((slug, f"{from_state}(recovered)", to_state))
            except Exception as exc:
                summary.errors.append(
                    (app, f"blocked-story recovery failed (non-fatal): {exc!r}")
                )

        stories = H.stories_in_flight(app, db)

        # Optional shard partitioning for safe multi-loop parallelism. Several
        # concurrent ``drive_chain`` loops each take the SAME ordered in-flight
        # snapshot and, as the queue drains, converge on the same front story —
        # racing to dispatch it into the same per-story worktree (observed:
        # three loops all running dev on story 16). There is no atomic claim at
        # dispatch, so the only safe way to run N loops is to give each a
        # DISJOINT slice of the story space. ``FACTORY_SHARD="k/n"`` keeps only
        # stories whose id ≡ k (mod n); run one loop per k with the same n and
        # the loops can never touch the same story. Unset → process everything
        # (single-loop default, unchanged behaviour).
        _shard = os.environ.get("FACTORY_SHARD", "").strip()
        if _shard:
            try:
                _k, _n = (int(x) for x in _shard.split("/"))
                if _n > 0:
                    stories = [s for s in stories if s.id is not None and s.id % _n == _k]
            except (ValueError, ZeroDivisionError):
                pass  # malformed shard spec → no filtering (fail-open)

        # ---- Signal: queue snapshot + spend snapshot ---------------------------
        try:
            from factory.manager.signals import write_queue_snapshot, write_spend_snapshot
            from factory.settings.spend import projected_end_of_day

            # Count stories by state for queue snapshot.
            _all_stories_for_app: list[StoryRecord] = []
            try:
                _eng_q = create_engine(f"sqlite:///{db}", echo=False)
                with Session(_eng_q) as _sess_q:
                    _all_stories_for_app = list(
                        _sess_q.exec(select(StoryRecord).where(StoryRecord.app == app)).all()
                    )
            except Exception:
                pass
            _counts: dict[str, int] = {}
            for _s in _all_stories_for_app:
                _counts[_s.state] = _counts.get(_s.state, 0) + 1
            write_queue_snapshot(app=app, counts_by_state=_counts, software_factory_root=root)

            # Spend snapshot — query by persona for by_persona breakdown.
            _today_usd = today_spend_usd(root, db_path=db)
            _hour_usd = hour_spend_usd(root, db_path=db)
            _proj_usd = projected_end_of_day(root, db_path=db)
            _daily_cap = float(getattr(settings.caps, "daily_spend_usd", 0) or 0)
            _hourly_cap = float(getattr(settings.caps, "hourly_spend_usd", 0) or 0)
            _by_persona: dict[str, float] = {}
            try:
                from factory.runner import Run as _Run

                _today_str = datetime.now(UTC).date().isoformat()
                _eng_sp = create_engine(f"sqlite:///{db}", echo=False)
                with Session(_eng_sp) as _sess_sp:
                    _run_rows = list(_sess_sp.exec(select(_Run)).all())
                for _r in _run_rows:
                    if (_r.ts or "").startswith(_today_str):
                        _by_persona[_r.persona] = _by_persona.get(_r.persona, 0.0) + float(
                            _r.cost_usd or 0.0
                        )
            except Exception:
                pass
            write_spend_snapshot(
                today_usd=_today_usd,
                last_hour_usd=_hour_usd,
                projected_eod_usd=_proj_usd,
                daily_cap_usd=_daily_cap,
                hourly_cap_usd=_hourly_cap,
                by_persona=_by_persona,
                software_factory_root=root,
            )
        except Exception:  # noqa: BLE001
            pass

        # Prune worktrees for stories that no longer need them (terminal
        # states, missing rows). Idempotent and best-effort — a failure here
        # mustn't take the tick down.
        if not dry_run:
            try:
                from factory.app_config import resolve_app_repo_path
                from factory.chain.worktree import prune_stale_worktrees

                active_ids: set[int] = {s.id for s in stories if s.id is not None}
                source_repo = resolve_app_repo_path(cfg, root)
                if source_repo.exists():
                    prune_stale_worktrees(
                        source_repo,
                        software_factory_root=root,
                        app=app,
                        active_story_ids=active_ids,
                    )
            except Exception as exc:
                summary.errors.append((app, f"worktree prune failed (non-fatal): {exc!r}"))

        # Even when no in-flight stories exist, we still want the
        # end-of-tick auto-merge hook to fire so PRs that landed in
        # PR_OPEN on a previous tick (and are therefore terminal here) get
        # a fresh merge attempt.
        for story in stories:
            # Advance up to ``max_advances_per_story`` steps for this story.
            for _ in range(max_advances_per_story):
                handler_name = _dispatch_for_story(story)
                if handler_name is None:
                    # No handler for this state — either in-progress (waiting on
                    # webhook) or terminal. Stop driving.
                    break

                # Dependency-ordering gate. A story does not build until every
                # lower-id story in its own direction is deployed (id order ==
                # SM build order: foundations first). This stops a dependent
                # story (smoke test / UI / endpoint) from being constructed
                # before the model/service/endpoint it relies on exists on
                # main — the out-of-order construction that stranded the
                # interdependent D008-D010 batch. Defer (not block): the story
                # waits in its current state until its foundations deploy.
                _deps_pending = _direction_deps_pending(db, story)
                if _deps_pending:
                    log_story_event(
                        story.id,
                        "dependency_deferred",
                        {
                            "direction": story.direction_id,
                            "waiting_on_story_ids": _deps_pending[:10],
                            "state": story.state,
                        },
                        software_factory_root=root,
                        slug_hint=story.slug,
                    )
                    break

                # Docs-chain serialization gate. A docs story may only LEAVE
                # STORY_CREATED when no other docs story for this app is already
                # active (open PR / mid-merge). This prevents two docs PRs —
                # which rewrite an overlapping set of canonical context files —
                # from being open at once and conflicting at merge time
                # (root cause of the blocked_deploy_failed docs backlog). The
                # gate fires only at the start state, so an already-running docs
                # story is never blocked by itself, and STORY_CREATED siblings
                # don't count as active → exactly one wins per tick, no deadlock.
                if (
                    story.chain_kind == "docs"
                    and story.state == StoryState.STORY_CREATED.value
                    and _count_app_docs_active(db, app, exclude_story_id=story.id) > 0
                ):
                    log_story_event(
                        story.id,
                        "docs_serialized",
                        {
                            "reason": "another docs story for this app has an "
                            "active PR; deferring to avoid context-file conflict",
                        },
                        software_factory_root=root,
                        slug_hint=story.slug,
                    )
                    break

                # Backpressure check before dispatch. The current_state dict is
                # recomputed each iteration so the in-flight counts reflect any
                # newly-completed stories. Use the cap-aware counter
                # (``_count_app_in_flight``) so queued ``STORY_CREATED`` siblings
                # spawned in the same PM-sync batch don't self-block the entire
                # batch — only stories with an active agent count.
                in_flight_app = _count_app_in_flight(db, app, exclude_story_id=story.id)
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
                    log_story_event(
                        story.id,
                        "dispatch_rejected",
                        {
                            "handler": handler_name,
                            "job_kind": job_kind,
                            "reason": decision.rejected_reason,
                            "global_in_flight": state_dict.get("global_in_flight"),
                            "app_in_flight": state_dict.get("app_in_flight"),
                            "today_spend_usd": state_dict.get("today_spend_usd"),
                            "hour_spend_usd": state_dict.get("hour_spend_usd"),
                        },
                        software_factory_root=root,
                        slug_hint=story.slug,
                    )
                    break
                # Job is allowed — clear any stale rejection reason.
                if story.last_rejection_reason is not None:
                    story.last_rejection_reason = None
                    H.persist_story(story, db)

                from_state = story.state
                log_story_event(
                    story.id,
                    "handler_start",
                    {
                        "handler": handler_name,
                        "from_state": from_state,
                        "model_tier": story.current_model_tier,
                        "dev_retries_so_far": story.dev_retries,
                    },
                    software_factory_root=root,
                    slug_hint=story.slug,
                )
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
                    # Roll the story back to its pre-handler state and stash the
                    # error on the StoryRecord. Handlers typically advance the
                    # story into a foo_in_progress state and persist it BEFORE
                    # invoking the LLM, so an exception leaves the row stuck —
                    # ``_dispatch_for_story`` returns None for *_in_progress
                    # states (those are webhook-driven), and the next tick can't
                    # retry. Rolling back makes handler crashes recoverable: the
                    # next tick will dispatch the same handler again.
                    story.state = from_state
                    story.error = repr(exc)
                    H.persist_story(story, db)
                    summary.errors.append((story.slug, repr(exc)))
                    log_story_event(
                        story.id,
                        "handler_exception",
                        {
                            "handler": handler_name,
                            "rolled_back_to": from_state,
                            "exception": repr(exc),
                        },
                        software_factory_root=root,
                        slug_hint=story.slug,
                    )
                    break
                summary.handler_runs.append((story.slug, from_state, story.state))
                summary.stories_advanced += 1
                log_story_event(
                    story.id,
                    "handler_end",
                    {
                        "handler": handler_name,
                        "from_state": from_state,
                        "to_state": story.state,
                        "had_error": bool(result.error),
                        "error": result.error,
                    },
                    software_factory_root=root,
                    slug_hint=story.slug,
                )
                if result.error or story.state == StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value:
                    summary.stories_blocked += 1
                    break
                if story.state == StoryState.PR_OPEN.value:
                    break

        # End-of-tick auto-merge hook. Runs after every story handler has had
        # its turn so a story that JUST advanced into PR_OPEN this tick gets
        # a merge attempt on the same tick. Gated by
        # ``factory_settings.auto_merge.enabled`` and skipped in modes where
        # forward motion is suppressed (``paused``, ``drain-reviews``).
        if settings.auto_merge.enabled:
            current_mode = get_mode(root, db_path=db)
            if current_mode not in {"paused", "drain-reviews"}:
                try:
                    merge_actions = auto_merge_tick(
                        root,
                        app,
                        dry_run=dry_run,
                        db_path=db,
                        merge_method=settings.auto_merge.merge_method,
                        wait_for_ci=settings.auto_merge.wait_for_ci,
                        delete_branch_after_merge=settings.auto_merge.delete_branch_after_merge,
                    )
                    summary.merges = merge_actions
                except Exception as exc:
                    # Auto-merge failures must not break the tick — the
                    # operator can still inspect the chain via ``factory
                    # story`` and re-run auto-merge by hand.
                    summary.errors.append(("auto-merge", repr(exc)))

        _tick_succeeded = True
    except Exception as _exc:  # noqa: BLE001
        _tick_exception = repr(_exc)
        raise
    finally:
        # ---- Signal: tick_end (guaranteed even on unhandled exceptions) ----
        try:
            from factory.manager.signals import write_tick_event as _wte

            _wte(
                "tick_end",
                tick_id=tick_id,
                app=app,
                dry_run=dry_run,
                duration_s=round(datetime.now(UTC).timestamp() - _tick_t0, 3),
                stories_advanced=summary.stories_advanced,
                stories_blocked=summary.stories_blocked,
                errors=len(summary.errors),
                merges_attempted=len(summary.merges),
                success=_tick_succeeded,
                exception=_tick_exception,
                software_factory_root=root,
            )
        except Exception:  # noqa: BLE001
            pass
        # Clean up the dry-run temp DB regardless of success or failure.
        if _dry_run_db_temp is not None:
            _dry_run_db_temp.unlink(missing_ok=True)

    return summary


def tick_summary_as_dict(summary: TickSummary) -> dict[str, Any]:
    return {
        "app": summary.app,
        "dry_run": summary.dry_run,
        "halted": summary.halted,
        "halt_reason": summary.halt_reason,
        "stories_advanced": summary.stories_advanced,
        "blocked_by_caps": summary.blocked_by_caps,
        "stories_blocked": summary.stories_blocked,
        "handler_runs": summary.handler_runs,
        "rejected": summary.rejected,
        "errors": summary.errors,
        "merges": [
            {
                "pr_number": m.pr_number,
                "merged": m.merged,
                "reason": m.reason,
                "gates_passed": list(m.gates_passed),
                "blocking_labels": list(m.blocking_labels),
            }
            for m in summary.merges
        ],
    }
