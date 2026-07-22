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

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, create_engine, select

from factory.app_config import AppConfig, load_app_config
from factory.chain import handlers as H
from factory.chain.auto_merge import MergeAction, auto_merge_tick
from factory.chain.ci_health import CiHealthResult, main_ci_health_tick
from factory.chain.event_log import log_story_event
from factory.chain.state_machine import (
    EVENT_BUDGET_EXCEEDED,
    StoryRecord,
    StoryState,
    advance,
)
from factory.chain.step_events import emit_chain_step
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
    # Rows deliberately quarantined this tick (e.g. an invalid-enum state from
    # a bad manual/manager write). NON-FATAL: they must NOT count toward the
    # failing exit code. A single poisoned row counted as an error crash-looped
    # the self-tick every cycle (2026-07-07, 2026-07-21); a stopped factory is
    # far worse than a quarantined row. Each skip is surfaced to stdout
    # (``skipped=N`` + a yellow line) and emitted once as an ``invalid_state_
    # skipped`` story event. NOTE: there is currently NO automatic reconciler
    # for invalid-enum rows and NO FMS escalation on the skip — a poisoned row
    # persists until an operator repairs it. Closing that gap (a reconcile
    # playbook + a watcher signal) is tracked as a follow-up; do not assume it
    # is handled elsewhere.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # End-of-tick auto-merge decisions (one entry per PR evaluated).
    # Empty when ``auto_merge.enabled=false`` or no PRs are eligible.
    merges: list[MergeAction] = field(default_factory=list)
    # Post-merge main-branch CI-health monitor result (D004). ``None`` when
    # ``ci_health.enabled=false`` or the factory mode suppresses it.
    ci_health: CiHealthResult | None = None
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


# WS1.1 per-story budget circuit breaker — the dispatch states that burn LLM
# spend. Membership gates the breaker: only a story ABOUT to enter one of these
# (via _dispatch_for_story) is checked. DERIVED from the single source of truth
# (``_DISPATCH``) rather than hand-listed, so a future dispatch state can't
# silently escape the breaker. DEPLOY_PENDING is the one deliberate exclusion:
# it is a dispatch state too, but a merged story must still be allowed to
# deploy — see the EVENT_BUDGET_EXCEEDED transition comment in state_machine.py
# (blocking deploy strands merged work without saving meaningful spend). Every
# state in this set MUST have an EVENT_BUDGET_EXCEEDED edge to
# BLOCKED_BUDGET_EXCEEDED in the transition table; both invariants (the derived
# set and full transition coverage) are asserted in
# tests/chain/test_per_story_budget.py so a new dispatch state fails a test
# rather than crashing a live tick.
_BUDGET_METERED_STATES: frozenset[StoryState] = frozenset(_DISPATCH) - {
    StoryState.DEPLOY_PENDING
}


def _story_ledger_spend_usd(db_path: Path, story_id: int | None) -> float | None:
    """Total ``runs.cost_usd`` attributed to ``story_id`` (D003 per-run ledger).

    This is the authoritative per-story spend: the run rows already carry the
    real cost of every LLM round-trip, so the breaker's ``total_spend_usd``
    accumulator is *derived* from the ledger rather than re-summed by hand
    (no double-counting on retries, self-healing after a crash).

    Returns 0.0 for an unsaved story (no id yet, no runs). Returns ``None`` on
    a read failure — the caller then KEEPS the prior accumulator instead of
    overwriting it. The manager daemon reads this same sqlite file
    concurrently, so a transient "database is locked" is expected and must
    NOT poison ``total_spend_usd``: writing a sentinel (an earlier version
    wrote ``inf``) persisted and then tripped the terminal breaker on a
    perfectly healthy story the next tick — and serialised as non-standard
    ``Infinity`` in the evidence ndjson. We retry a few times to ride out a
    lock, then give up for this cycle; the attempts cap remains the backstop
    while spend is briefly unreadable, so the breaker never spends blind.
    """
    if story_id is None:
        return 0.0
    import time

    from factory.runner import Run

    for attempt in range(3):
        try:
            eng = create_engine(f"sqlite:///{db_path}", echo=False)
            total = 0.0
            with Session(eng) as session:
                for r in session.exec(select(Run).where(Run.story_id == story_id)).all():
                    total += float(r.cost_usd or 0.0)
            return total
        except Exception:  # noqa: BLE001 — transient lock/contention; retry then skip.
            time.sleep(0.05 * (attempt + 1))
    return None


# WS1.1 advance-decay: a canonical monotonic ranking of the happy-path states.
# "Genuine forward progress" == entering a state whose ordinal EXCEEDS the
# highest the story has ever reached (``story.max_progress_ordinal``). The
# ranking is deliberately COARSE around the dev<->review loop: DEV_IN_PROGRESS,
# DEV_RETRY, and REVIEWER_REQUESTED_CHANGES all sit at/below the reviewer tier,
# so a dev<->reviewer ping-pong (tests_green -> reviewer -> requested_changes ->
# dev -> tests_green -> ...) re-treads states already at the high-water mark and
# NEVER counts as progress — exactly the oscillation the breaker must still be
# able to trip. Only crossing a genuine NEW milestone (first tests_green, first
# reviewer pass, an APPROVED review, tech-writer, docs, PR, deploy) decays the
# attempt counter. Error/blocked/*_in_progress-only states are absent -> ordinal
# 0 (never progress). Stored as an int on the story, so the map's VALUES must
# stay stable across versions even if states are added.
_STATE_PROGRESS_ORDINAL: dict[StoryState, int] = {
    StoryState.STORY_CREATED: 1,
    StoryState.SM_IN_PROGRESS: 2,
    StoryState.DOCS_SM_IN_PROGRESS: 2,
    StoryState.SM_DONE: 3,
    StoryState.DOCS_SM_DONE: 3,
    StoryState.DEV_IN_PROGRESS: 4,
    StoryState.DEV_RETRY: 4,  # a retry is NOT progress — same tier as dev
    StoryState.DOCS_ONBOARDER_IN_PROGRESS: 4,
    StoryState.TESTS_GREEN: 5,
    StoryState.REVIEWER_IN_PROGRESS: 6,
    # request-changes bounces back to dev; keep it AT the reviewer tier so the
    # oscillation stays flat (never re-decays once the reviewer tier is reached).
    StoryState.REVIEWER_REQUESTED_CHANGES: 6,
    StoryState.REVIEWER_DONE: 7,  # approved — a genuine milestone
    StoryState.TECH_WRITER_IN_PROGRESS: 8,
    StoryState.TECH_WRITER_DONE: 9,
    StoryState.DOCS_ONBOARDER_DONE: 9,
    StoryState.DOCS_ENFORCER_CHECK: 10,
    StoryState.PR_OPEN: 11,
    StoryState.CI_PENDING: 12,
    StoryState.CI_GREEN: 13,
    StoryState.READY_FOR_MERGE: 14,
    StoryState.DEPLOY_PENDING: 15,
    StoryState.DEPLOYED: 16,
}


def _progress_ordinal(state_value: str) -> int:
    """Happy-path progress ordinal for a state value (0 for error/blocked/unknown)."""
    try:
        return _STATE_PROGRESS_ORDINAL.get(StoryState(state_value), 0)
    except ValueError:
        return 0


def _apply_advance_decay(story: StoryRecord) -> bool:
    """Reset ``total_attempts`` iff ``story`` just made genuine forward progress.

    Genuine progress == the story's CURRENT state ranks strictly higher than any
    state it has previously reached (``max_progress_ordinal``). This is
    monotonic, so each milestone decays the attempt counter AT MOST once and a
    dev<->review oscillation (which never exceeds the high-water mark) never
    decays — so an oscillating/stuck story still exhausts the attempt budget
    while an advancing one is never tripped on attempts. ``total_spend_usd`` is
    deliberately untouched: spend is the absolute cost ceiling.

    Returns True when a decay was applied (caller logs an evidence event).
    """
    ordinal = _progress_ordinal(story.state)
    if ordinal > story.max_progress_ordinal:
        story.max_progress_ordinal = ordinal
        story.total_attempts = 0
        return True
    return False


def _story_budget_breaker_reason(story: StoryRecord, caps: Any) -> str | None:
    """Return a human-readable reason iff the per-story breaker has tripped.

    Pure: reads only the accumulator fields on ``story`` and the caps. The
    caller is responsible for the state transition + evidence event. Only
    budget-metered states are checked so a story that reached DEPLOY_PENDING
    (or a terminal state) is never budget-blocked.
    """
    if StoryState(story.state) not in _BUDGET_METERED_STATES:
        return None
    per_story_attempts = int(getattr(caps, "per_story_attempts", 0) or 0)
    per_story_spend = float(getattr(caps, "per_story_spend_usd", 0.0) or 0.0)
    if per_story_attempts > 0 and story.total_attempts >= per_story_attempts:
        return f"total_attempts={story.total_attempts} >= per_story_attempts={per_story_attempts}"
    if per_story_spend > 0 and story.total_spend_usd >= per_story_spend:
        return (
            f"total_spend_usd={story.total_spend_usd:.4f} >= "
            f"per_story_spend_usd={per_story_spend}"
        )
    return None


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
    # WS1.1 terminal budget sink — the story is done burning spend; it must
    # not count against concurrency caps.
    StoryState.BLOCKED_BUDGET_EXCEEDED.value,
    # Dual-draft loser sink (terminal). A superseded sibling is abandoned; it
    # must not consume a concurrency slot.
    StoryState.SUPERSEDED_BY_SIBLING.value,
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


def _normalize_failure_text(raw: str) -> str:
    """Strip volatile bits (timestamps, paths, durations, addresses/ids) from
    ``raw`` failure text so a re-run that fails for the identical reason
    produces identical normalized text even though wall-clock/paths differ
    between attempts.

    Factored out of ``_story_failure_signature`` so ``auto_merge``'s real-CI
    failure signature (a different failure-text source: a ``gh run view
    --log-failed`` digest instead of a dev/review test-output tail) can reuse
    the exact same "is this the SAME failure" normalization rather than
    maintaining a parallel copy of the regex list.
    """
    normalized = re.sub(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?",
        "<ts>",
        raw,
    )
    normalized = re.sub(r"(?:/[\w.\-]+){2,}", "<path>", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?s\b", "<dur>", normalized)
    normalized = re.sub(r"\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b", "<dur>", normalized)
    # Strip non-deterministic identifiers so the SAME logical failure hashes
    # identically across cycles: hex memory addresses / object ids
    # (0x7f..., id=140234..., "at 0x..."), and generic long hex/uuid runs that
    # appear in mock/object reprs and temp names. Without this the guard fails
    # OPEN on failures whose tail embeds an address and never detects the loop.
    normalized = re.sub(r"0x[0-9a-fA-F]+", "<addr>", normalized)
    normalized = re.sub(r"\bid=\d+", "id=<id>", normalized)
    normalized = re.sub(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        "<uuid>",
        normalized,
    )
    normalized = re.sub(r"\b[0-9a-fA-F]{12,}\b", "<hex>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _story_failure_signature(story: StoryRecord) -> str:
    """Return a normalized signature of ``story``'s most recent failure.

    Prefers the last ``dev_attempts_json`` entry's ``test_output_tail``
    (the freshest evidence of *why* dev/review failed — see
    ``handlers._fetch_latest_test_output`` for the same preference order),
    falling back to ``story.error`` when no attempts are on record.

    Volatile bits (timestamps, absolute paths, durations) are stripped so
    a re-run that fails for the identical reason produces an identical
    signature even though wall-clock/paths differ between attempts — that
    identity is what lets ``_recover_blocked_stories`` detect "no new
    signal" and stop hamster-wheeling a structurally unsatisfiable story.

    Returns ``""`` when no failure text is available (e.g. brand-new story).
    """
    raw = ""
    if story.dev_attempts_json:
        try:
            attempts = json.loads(story.dev_attempts_json)
        except (TypeError, ValueError):
            attempts = None
        if isinstance(attempts, list) and attempts:
            last = attempts[-1]
            if isinstance(last, dict):
                raw = (last.get("test_output_tail") or "").strip()
    if not raw:
        raw = (story.error or "").strip()
    return _failure_signature_from_tail(raw)


def _failure_signature_from_tail(raw: str) -> str:
    """Normalized failure signature for a raw failure/test-output tail.

    The tail→normalize→hash core of ``_story_failure_signature``, exposed so the
    dev loop can sign an attempt's own ``test_output_tail`` directly (it already
    has the tail in-memory and must not re-read the story's serialized state).
    Both callers therefore produce the IDENTICAL signature for the same failure.
    Returns ``""`` when there is no failure text.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    normalized = _normalize_failure_text(raw)
    # The tail carries the actual assertion/error; the head is often
    # boilerplate pytest banner/collection noise that's identical across
    # unrelated failures.
    return hashlib.sha256(normalized[-500:].encode("utf-8")).hexdigest()


def _recover_blocked_stories(
    db: Path, app: str, *, root: Path
) -> list[tuple[str, str, str]]:
    """Re-dispatch blocked stories so since-shipped chain fixes reach them.

    For each story in an auto-recoverable blocked state, reset it to the
    re-entry point (SM_DONE → dev) with retry/cycle counters cleared (the
    last reviewer findings are kept — see inline comment) so it flows through the current
    chain from scratch. Bounded to ``_MAX_AUTO_RECOVERIES``
    per story via ``auto_recovery`` events in the per-story log; once exhausted
    the story stays blocked and an ``auto_recovery_exhausted`` /
    ``factory_needs_redesign`` event fires so the FMS/operator sees a genuinely
    stuck story rather than an endless recycle.

    Pure DB rewrite — no LLM/git work — mirroring ``_prune_stale_in_progress``.
    Returns (slug, from_state, to_state) tuples for the TickSummary.

    Signal-changed guard: a recovery also requires that the story's current
    failure differs from the one recorded at its *most recent* prior
    recovery (see ``_story_failure_signature``). Recovering blindly on the
    identical failure just burns a full dev cycle (up to 6 retries) to
    rediscover the same dead end, so an unchanged signature escalates
    immediately instead of consuming another slot of ``_MAX_AUTO_RECOVERIES``.
    A story making genuine progress (a different failure each time) still
    gets its recoveries; the first recovery is always allowed.
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

        events = read_story_events(story.id, software_factory_root=root, slug_hint=story.slug)

        # Only recoveries into the CURRENT re-entry point consume the budget.
        # When the chain is redesigned the re-entry target changes (e.g. the
        # old test-first regime re-entered at tests_red; Loop-4 re-enters at
        # sm_done), and attempts burnt under the old regime say nothing about
        # whether the new chain can converge the story — the budget resets.
        prior_recoveries = [
            e for e in events if e.get("event") == "auto_recovery" and e.get("to_state") == target
        ]
        prior = len(prior_recoveries)

        already_escalated = any(e.get("event") == "auto_recovery_exhausted" for e in events)

        if prior >= _MAX_AUTO_RECOVERIES:
            # Already re-attempted the allowed number of times and still
            # blocked → genuinely stuck. Emit a loud, deduped escalation once.
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

        signature = _story_failure_signature(story)

        if prior_recoveries:
            last_signature = prior_recoveries[-1].get("failure_signature")
            if signature and last_signature is not None and signature == last_signature:
                # The last recovery attempt produced the EXACT same failure —
                # nothing changed. Recovering again would just grind through
                # another full dev cycle for no new signal; escalate instead
                # (deduped, same as the cap-exhausted path above).
                if not already_escalated:
                    log_story_event(
                        story.id,
                        "auto_recovery_exhausted",
                        {
                            "state": story.state,
                            "recoveries": prior,
                            "error": (story.error or "")[:300],
                            "reason": "identical_failure_signature",
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
        # Deliberately KEEP reviewer_result_json: the last reviewer verdict is
        # the record of why the story blocked, the worktree still contains the
        # rejected code, and handle_dev feeds findings into the prompt whenever
        # they exist — so the first post-recovery dev pass starts informed
        # instead of burning a cycle rediscovering the same objections.
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
                "failure_signature": signature,
            },
            software_factory_root=root,
            slug_hint=story.slug,
        )

    return recovered


# Bound on how many stories ``reconcile_from_github`` will query GitHub for in a
# single tick. Each candidate costs one read-only ``gh pr view`` shell-out;
# capping keeps a large PR backlog from turning every tick into a burst of API
# calls. Candidates beyond the cap are simply reconciled on a LATER tick (they
# are never lost — they stay in a mergeable state and remain candidates).
_MAX_RECONCILE_PER_TICK = 25


def _query_pr_state(*, app_config: AppConfig, pr_number: int) -> str | None:
    """Authoritative GitHub state for ``pr_number``.

    Returns ``"OPEN"``, ``"CLOSED"``, ``"MERGED"``, or ``None`` when the state
    cannot be determined. Read-only ``gh pr view --json state`` shell-out — the
    SAME plumbing ``auto_merge._pr_terminally_unmergeable`` uses (gh's ``state``
    field returns exactly those three literals; ``MERGED`` is distinct from a
    plain ``CLOSED``).

    ``None`` is the fail-safe sentinel: a non-positive placeholder PR number, gh
    missing, a timeout, a non-zero exit (PR deleted / wrong repo / auth), or an
    unparseable payload all map to ``None`` so the caller NEVER reconciles a
    story on an uncertain answer.
    """
    import subprocess

    if pr_number <= 0:  # synthesized placeholder — nothing to query
        return None
    cmd = [
        "gh", "pr", "view", str(pr_number), "--repo", app_config.repo,
        "--json", "state",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        # gh could not resolve the PR (deleted / wrong repo / auth). Unknown —
        # do not reconcile on uncertainty.
        return None
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None
    state = str(data.get("state", "")).upper()
    if state in ("OPEN", "CLOSED", "MERGED"):
        return state
    return None


def _write_drift_event(
    *,
    root: Path,
    story: StoryRecord,
    from_state: str,
    pr_state: str,
    action: str,
) -> None:
    """Emit a first-class ``state_drift_reconciled`` anomaly (best-effort).

    Written to the ``git`` signal stream (one of the L1 watcher's ``_RAW_STREAMS``
    — so drift is SEEN by the FMS, not silent) AND to the per-story event log for
    the story timeline. Telemetry only: never raises, so a logging failure cannot
    crash the tick.
    """
    try:
        from factory.manager.signals import write_event

        write_event(
            "git",
            {
                "event": "state_drift_reconciled",
                "app": story.app,
                "story_id": story.id,
                "slug": story.slug,
                "pr_number": story.github_pr_number,
                "local_state_before": from_state,
                "authoritative_pr_state": pr_state,
                "action": action,
            },
            software_factory_root=root,
        )
    except Exception:  # noqa: BLE001 - telemetry, never crash the tick
        pass
    try:
        from factory.chain.event_log import log_story_event

        log_story_event(
            story.id,
            "state_drift_reconciled",
            {
                "local_state_before": from_state,
                "authoritative_pr_state": pr_state,
                "action": action,
                "pr_number": story.github_pr_number,
            },
            software_factory_root=root,
            slug_hint=story.slug,
        )
    except Exception:  # noqa: BLE001
        pass


def _record_reconciled_merge_and_enqueue_deploy(
    *, app: str, story: StoryRecord, pr_number: int, db: Path, root: Path
) -> None:
    """Record a ``merged=True`` merge-action row + enqueue a deploy for a merge
    that ``reconcile_from_github`` detected on GitHub (best-effort, idempotent).

    Since the auto-merge worker now only claims ``merged=True`` on a REALLY
    merged PR (``--auto`` merely ENABLES async auto-merge — it does not merge
    now), ``reconcile_from_github`` becomes the PRIMARY detector of the real,
    asynchronous merge. So it must trigger the deploy exactly like auto-merge's
    own merged path does — otherwise a merge that lands between ticks advances
    the story to ``deploy_pending`` but nothing ever deploys it.

    ``head_sha`` uses the SAME ``local-<story.id>`` scheme the auto-merge
    worker's synthesized production path uses (StoryRecord carries no real head
    sha, and the deploy sha is only a dedup/label key — the deploy pulls the
    merged branch, it does not check out this sha). Sharing the scheme makes the
    two merge detectors DEDUPE against each other: at most one row per story.

    Idempotent + fail-safe: only records/enqueues when no ``merged=True`` row
    for this ``head_sha`` already exists, and never raises (a recorder/enqueue
    hiccup must not break reconcile). A story leaves ``_MERGEABLE_STATES`` the
    moment it is reconciled, so it is no longer a candidate on later ticks — the
    dedupe check is belt-and-suspenders against a same-tick race with the
    auto-merge worker.
    """
    from factory.chain.auto_merge import (
        MergeAction,
        MergeActionRecord,
        _record_merge_action,
    )

    head_sha = f"local-{story.id}"
    try:
        eng = create_engine(f"sqlite:///{db}", echo=False)
        with Session(eng) as session:
            existing = session.exec(
                select(MergeActionRecord).where(
                    MergeActionRecord.app == app,
                    MergeActionRecord.head_sha == head_sha,
                    MergeActionRecord.merged == True,  # noqa: E712
                )
            ).first()
        if existing is not None:
            return  # already recorded (and deploy already enqueued) — no-op
        action = MergeAction(
            app=app,
            pr_number=pr_number,
            merged=True,
            reason="reconcile: PR merged on GitHub",
        )
        _record_merge_action(action, head_sha, db)
    except Exception:  # noqa: BLE001 - fail-safe: never break reconcile
        return

    try:
        from factory.deploy.orchestrator import enqueue_deploy

        enqueue_deploy(
            app=app,
            sha=head_sha,
            merged_pr_number=pr_number,
            software_factory_root=root,
            db_path=db,
        )
    except Exception:  # noqa: BLE001 - deploy-enqueue hiccup must not break reconcile
        pass


def _current_story_state(db: Path, story_id: int | None) -> str | None:
    """Read a story's LIVE state from the DB (the reconcile ``candidates``
    snapshot goes stale as siblings are superseded mid-loop). Returns None on
    any failure or missing id — the caller treats None as "no fresh info, use
    the snapshot", which is fail-safe (never skips on uncertainty)."""
    if story_id is None:
        return None
    try:
        eng = create_engine(f"sqlite:///{db}", echo=False)
        with Session(eng) as session:
            row = session.exec(
                select(StoryRecord).where(StoryRecord.id == story_id)
            ).first()
            return row.state if row is not None else None
    except Exception:  # noqa: BLE001 - a read hiccup must never break reconcile
        return None


def _close_dual_draft_sibling_on_reconcile(
    *,
    winner: StoryRecord,
    cfg: AppConfig,
    root: Path,
    db: Path,
    github_client_factory: Callable[[], Any] | None,
    runner: Any,
) -> None:
    """Retire the losing dual-draft sibling when RECONCILE detects the winner's
    merge — the reconcile-path analogue of the auto-merge worker's own
    ``close_abandoned_draft_sibling`` call (auto_merge_tick, ``if action.merged``).

    Why this exists: fix A routes the real, asynchronous ``gh pr merge --auto``
    merge through ``reconcile_from_github`` (auto-merge only ENABLES auto-merge,
    returning ``merged=False``). Reconcile is therefore the PRIMARY detector of
    the merge for ``--auto`` PRs — the now-common case — and the ONLY place the
    winner's merge is observed on that path. Without this call the losing
    sibling is never superseded and proceeds toward a redundant second merge
    (the dual-draft OVER-FIRE that #70 meant to fix but only closed for the rare
    synchronous auto-merge path).

    Best-effort + idempotent: skips a non-dual-draft winner before building any
    GitHub client; ``close_abandoned_draft_sibling`` skips an already-superseded
    sibling and never raises. Any error here is swallowed — sibling cleanup must
    never break reconcile. ``dry_run`` is False: reconcile only runs on the
    real-run (``not dry_run``) tick path.
    """
    try:
        from factory.chain.dual_draft import (
            _draft_alt_suffix,
            close_abandoned_draft_sibling,
        )

        # Short-circuit before constructing a client: the vast majority of
        # merges are NOT dual-draft, and we should not resolve a GitHub token /
        # build a client for them.
        if _draft_alt_suffix(getattr(winner, "slug", "") or "") is None:
            return

        factory = github_client_factory
        if factory is None:
            from factory.providers.github import build_github_client

            factory = build_github_client
        gh = factory()
        if gh is None:
            # No token / client available — cleanup is best-effort; the Part 2
            # self-check in auto-merge still blocks the loser from merging.
            return
        close_abandoned_draft_sibling(
            winner, cfg, root, db, gh, False, runner=runner
        )
    except Exception:  # noqa: BLE001 - cleanup must never break reconcile
        pass


def reconcile_from_github(
    db: Path,
    app: str,
    *,
    cfg: AppConfig,
    root: Path,
    max_reconcile: int = _MAX_RECONCILE_PER_TICK,
    query_pr_state: Callable[..., str | None] = _query_pr_state,
    github_client_factory: Callable[[], Any] | None = None,
    sibling_cleanup_runner: Any = None,
) -> list[tuple[str, str, str]]:
    """Pull authoritative GitHub PR state into the local DB at the top of a tick.

    Local ``factory.db`` state is a PROJECTION; GitHub is the system of record
    for whether a PR merged, closed, or is still open. That projection drifts: a
    PR merged (or completed out-of-band) while the local story still says
    ``pr_open``; a PR closed while the story keeps looping on a dead branch. This
    pass reconciles each non-terminal story that has a real PR against GitHub
    BEFORE any dispatch decision, using the SAME state-machine transitions
    auto-merge uses, and logs every reconciliation as a first-class
    ``state_drift_reconciled`` anomaly so drift is never silent.

    Candidates are stories in ``auto_merge._MERGEABLE_STATES``
    (``pr_open`` / ``ci_green`` / ``ready_for_merge``) with a positive
    ``github_pr_number`` — exactly the states that hold an open PR and for which
    the ``EVENT_MERGED`` / ``EVENT_PR_UNMERGEABLE`` transitions are defined.

    Drift cases handled:

    * PR **MERGED** on GitHub, local state still pre-merge → ``advance(story,
      EVENT_MERGED)`` → ``DEPLOY_PENDING`` (identical to the auto-merge success
      path) so the missed merge flows into deploy instead of being re-attempted
      forever. Because auto-merge now only claims ``merged=True`` on a REAL
      merge (``--auto`` merely enables async auto-merge), reconcile is the
      PRIMARY detector of the async merge and ALSO records a ``merged=True``
      merge-action row + enqueues the deploy (see
      ``_record_reconciled_merge_and_enqueue_deploy``) so the app actually ships.
    * PR **CLOSED** (not merged) on GitHub, local state still in-flight →
      ``advance(story, EVENT_PR_UNMERGEABLE)`` → ``BLOCKED_DEPLOY_FAILED`` so the
      story stops looping on a dead PR and surfaces for attention.
    * PR **OPEN** → local projection already matches GitHub → no-op.
    * **Unknown** query result (``None``) → no-op. Fail-safe: never advance a
      story on an ambiguous or failed GitHub query.

    Idempotent: once reconciled the story leaves ``_MERGEABLE_STATES`` and is no
    longer a candidate, so a consistent DB produces zero mutations and zero
    events on re-run. Bounded: at most ``max_reconcile`` GitHub calls per tick.
    Pure DB rewrite + read-only gh queries — no LLM / git-write work, mirroring
    ``_recover_blocked_stories``. Returns ``(slug, from_state, to_state)`` tuples
    for the TickSummary.
    """
    from factory.chain.auto_merge import _MERGEABLE_STATES
    from factory.chain.handlers import persist_story
    from factory.chain.state_machine import (
        EVENT_MERGED,
        EVENT_PR_UNMERGEABLE,
        IllegalTransitionError,
    )

    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        candidates = session.exec(
            select(StoryRecord).where(
                StoryRecord.app == app,
                StoryRecord.state.in_(list(_MERGEABLE_STATES)),  # type: ignore[attr-defined]
            )
        ).all()

    reconciled: list[tuple[str, str, str]] = []
    checked = 0
    for story in candidates:
        pr_number = story.github_pr_number
        if pr_number is None or pr_number <= 0:
            continue
        if checked >= max_reconcile:
            break

        # The ``candidates`` snapshot is taken ONCE, but a winner processed
        # earlier in THIS same loop may have run the dual-draft cleanup, which
        # closes a losing sibling's PR and sets it to SUPERSEDED_BY_SIBLING.
        # Re-read the live state and skip any candidate that has already left
        # _MERGEABLE_STATES — otherwise we'd query its just-closed PR, see
        # CLOSED, apply EVENT_PR_UNMERGEABLE, and clobber the intended
        # SUPERSEDED_BY_SIBLING with a false BLOCKED_DEPLOY_FAILED "attention"
        # signal (adversarial review, 2026-07-21).
        live_state = _current_story_state(db, story.id)
        if live_state is not None and live_state not in _MERGEABLE_STATES:
            continue
        if live_state is not None:
            story.state = live_state

        checked += 1
        pr_state = query_pr_state(app_config=cfg, pr_number=pr_number)
        if pr_state is None or pr_state == "OPEN":
            # Unknown → fail-safe no-op (never advance on uncertainty).
            # OPEN → local projection already matches GitHub → no-op.
            continue

        from_state = story.state
        event = EVENT_MERGED if pr_state == "MERGED" else EVENT_PR_UNMERGEABLE
        try:
            new_state = advance(story, event)
        except IllegalTransitionError:
            # No transition for this (state, event) pair — surface the observed
            # drift but do NOT force an illegal mutation.
            _write_drift_event(
                root=root,
                story=story,
                from_state=from_state,
                pr_state=pr_state,
                action="observed_no_transition",
            )
            continue

        story.state = new_state.value
        if event == EVENT_PR_UNMERGEABLE:
            story.error = (
                f"reconcile: PR #{pr_number} is CLOSED on GitHub (not merged) "
                f"while local state was {from_state!r}; routed to "
                f"{new_state.value} for attention."
            )
        persist_story(story, db)
        if event == EVENT_MERGED:
            # Reconcile is the PRIMARY detector of the real (async) merge now
            # that auto-merge no longer claims merged=True on mere
            # auto-merge-enable. Trigger the deploy exactly like auto-merge's
            # merged path so ``_latest_undeployed_sha`` picks it up and the app
            # actually deploys. Best-effort + idempotent; never crashes reconcile.
            _record_reconciled_merge_and_enqueue_deploy(
                app=app, story=story, pr_number=pr_number, db=db, root=root
            )
            # Dual-draft cleanup on the RECONCILE path (Part 1). Because fix A
            # routes the async ``--auto`` merge through reconcile, this — not the
            # auto-merge worker — is where the winner's merge is observed for the
            # common case, so the losing sibling must be superseded HERE too, the
            # same way ``auto_merge_tick`` does on its own merged path. Best-
            # effort/idempotent; never raises out of reconcile.
            _close_dual_draft_sibling_on_reconcile(
                winner=story,
                cfg=cfg,
                root=root,
                db=db,
                github_client_factory=github_client_factory,
                runner=sibling_cleanup_runner,
            )
        reconciled.append((story.slug, from_state, new_state.value))
        _write_drift_event(
            root=root,
            story=story,
            from_state=from_state,
            pr_state=pr_state,
            action=f"advanced_to:{new_state.value}",
        )

    return reconciled


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
        # The dangerous data path (a corrupt/unreadable halt FILE) now fails
        # SAFE inside halt.is_halted itself. This guard only fires when the halt
        # MODULE is broken (e.g. ImportError). We keep fail-open here — halting
        # all ticks on an import error would wedge the factory with no recovery
        # path — but we make it a CRITICAL, visible alert (not a stderr line the
        # FMS can't see) so the broken module gets fixed.
        try:
            from factory.manager.signals import write_alert_event

            write_alert_event(
                "halt_check_module_error",
                f"halt-check raised {_halt_exc!r}; continuing with tick "
                "(fail-open). Indicates a broken halt module, not a corrupt "
                "halt file (that path fails safe).",
                severity="critical",
                software_factory_root=root,
            )
        except Exception:  # noqa: BLE001 - alerting is best-effort
            import sys as _sys
            print(
                f"[orchestrator] CRITICAL: halt-check raised {_halt_exc!r} "
                "(fail-open) and alert emit failed.",
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
            # Reconcile-from-authoritative FIRST: local factory.db state is a
            # PROJECTION; GitHub is the system of record for PR merge/close truth.
            # Pull authoritative PR state into the DB BEFORE any recovery or
            # dispatch decision, so a merge that happened out-of-band flows into
            # deploy (and a dead/closed PR sinks to attention) instead of the
            # recovery/dispatch logic acting on stale local state. Read-only gh
            # queries + pure DB rewrite; a gh failure is a fail-safe no-op.
            try:
                drifted = reconcile_from_github(db, app, cfg=cfg, root=root)
                for slug, from_state, to_state in drifted:
                    summary.handler_runs.append((slug, f"{from_state}(drift)", to_state))
            except Exception as exc:
                summary.errors.append(
                    (app, f"github reconcile failed (non-fatal): {exc!r}")
                )

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
                # KEEP worktrees of blocked stories: blocked states mean
                # "awaiting operator resolution or auto-recovery", and both
                # use the worktree — the operator resolves merge conflicts in
                # it, and recovery re-dispatches dev into it. Pruning them
                # destroyed in-progress operator conflict resolutions three
                # times on 2026-06-11/12. Only DEPLOYED (and rows gone from
                # the DB) are truly done with their worktree.
                eng_keep = create_engine(f"sqlite:///{db}", echo=False)
                with Session(eng_keep) as _ks:
                    blocked_rows = _ks.exec(
                        select(StoryRecord).where(
                            StoryRecord.app == app,
                            StoryRecord.state.in_(  # type: ignore[attr-defined]
                                [
                                    StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value,
                                    StoryState.BLOCKED_DEPLOY_FAILED.value,
                                    StoryState.BLOCKED_REVIEW_NONCONVERGENT.value,
                                ]
                            ),
                        )
                    ).all()
                active_ids |= {r.id for r in blocked_rows if r.id is not None}
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

        # START-of-tick auto-merge pass. With in-tick dev convergence, a tick
        # can run for hours; PRs that reached PR_OPEN on a PREVIOUS tick must
        # not wait for this tick's story loop to finish before merging
        # (observed 2026-07-18: two mergeable PRs sat >2h behind a long tick).
        # The end-of-tick hook below still runs for PRs that open THIS tick.
        if settings.auto_merge.enabled:
            _pre_mode = get_mode(root, db_path=db)
            if _pre_mode not in {"paused", "drain-reviews"}:
                try:
                    summary.merges = auto_merge_tick(
                        root,
                        app,
                        dry_run=dry_run,
                        db_path=db,
                        merge_method=settings.auto_merge.merge_method,
                        wait_for_ci=settings.auto_merge.wait_for_ci,
                        delete_branch_after_merge=settings.auto_merge.delete_branch_after_merge,
                    )
                except Exception as exc:
                    summary.errors.append(("auto-merge-pre", repr(exc)))

        # Even when no in-flight stories exist, we still want the
        # end-of-tick auto-merge hook to fire so PRs that landed in
        # PR_OPEN on a previous tick (and are therefore terminal here) get
        # a fresh merge attempt.
        for story in stories:
            # Poisoned-row guard: a state value outside the StoryState enum
            # (e.g. a bad manual/manager write) must quarantine THAT story,
            # not abort the whole tick — one invalid row halted the factory
            # for days on 2026-07-07. Skip it, surface it, keep ticking.
            try:
                StoryState(story.state)
            except ValueError:
                summary.skipped.append(
                    (story.slug, f"invalid state {story.state!r}; story skipped (non-fatal)")
                )
                # Emit the anomaly ONCE per (story, bad-state), not every tick:
                # a poisoned row persists until an operator repairs it, and the
                # 5-min timer would otherwise append this event ~288x/day/row
                # forever (unbounded per-story-log growth). Dedup mirrors
                # _handle_ci_failure's already-escalated guard.
                if story.id is not None:
                    from factory.chain.event_log import read_story_events as _rse

                    _prior = _rse(
                        story.id, software_factory_root=root, slug_hint=story.slug
                    )
                    _already = any(
                        e.get("event") == "invalid_state_skipped"
                        and e.get("state") == story.state
                        for e in _prior
                    )
                    if not _already:
                        log_story_event(
                            story.id,
                            "invalid_state_skipped",
                            {"state": story.state},
                            software_factory_root=root,
                            slug_hint=story.slug,
                        )
                continue
            # Advance up to ``max_advances_per_story`` steps for this story.
            for _ in range(max_advances_per_story):
                handler_name = _dispatch_for_story(story)
                if handler_name is None:
                    # No handler for this state — either in-progress (waiting on
                    # webhook) or terminal. Stop driving.
                    break

                # ---- WS1.1 GLOBAL per-story budget circuit breaker ----------
                # BEFORE dispatching the handler, check the aggregate per-story
                # ceiling. The composed loops (dev retries, reviewer cycles,
                # auto-recovery re-dispatch, CI-fix) each have their own counter
                # but no shared ceiling; a pathological story can burn the
                # product of all of them. If the story's total attempts or total
                # spend has crossed the per-story cap, route it to the terminal
                # BLOCKED_BUDGET_EXCEEDED sink and emit an EVIDENCE event — never
                # silently drop. Terminal (no auto-recovery back into the loop)
                # so a broken story stops burning spend.
                _budget_reason = _story_budget_breaker_reason(story, settings.caps)
                if _budget_reason is not None:
                    from_state = story.state
                    story.state = advance(story, EVENT_BUDGET_EXCEEDED).value
                    story.last_rejection_reason = _budget_reason
                    H.persist_story(story, db)
                    _last_signature = story.error or story.last_rejection_reason
                    log_story_event(
                        story.id,
                        "budget_exceeded",
                        {
                            "story_id": story.id,
                            "slug": story.slug,
                            "handler": handler_name,
                            "from_state": from_state,
                            "to_state": story.state,
                            "reason": _budget_reason,
                            "total_attempts": story.total_attempts,
                            "total_spend_usd": round(story.total_spend_usd, 4),
                            "per_story_attempts": getattr(
                                settings.caps, "per_story_attempts", None
                            ),
                            "per_story_spend_usd": getattr(
                                settings.caps, "per_story_spend_usd", None
                            ),
                            "dev_retries": story.dev_retries,
                            "reviewer_cycles": story.reviewer_cycles,
                            "last_failure_signature": _last_signature,
                        },
                        software_factory_root=root,
                        slug_hint=story.slug,
                    )
                    summary.handler_runs.append((story.slug, from_state, story.state))
                    summary.stories_blocked += 1
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
                # WS1.1 breaker accumulator: count this dispatch. Bumped BEFORE
                # invoking so a handler that crashes still counts as a burned
                # attempt (the pre-dispatch check above reads this next tick).
                story.total_attempts += 1
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
                    # Refresh the breaker's spend accumulator from the ledger:
                    # a crashed handler may still have recorded partial run cost.
                    # None == transient read failure → keep the prior value
                    # (never poison the accumulator with a sentinel).
                    _spend = _story_ledger_spend_usd(db, story.id)
                    if _spend is not None:
                        story.total_spend_usd = _spend
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
                    # WS4.2: record the failed dispatch as a chain_step too, so
                    # the replayable stream captures crashes, not just advances.
                    emit_chain_step(
                        story,
                        handler=handler_name,
                        from_state=from_state,
                        to_state=story.state,
                        outcome="exception",
                        software_factory_root=root,
                        extra={"exception": repr(exc)},
                    )
                    break
                # WS1.1 breaker accumulator: refresh this story's aggregate
                # spend from the D003 per-run ledger (authoritative — no
                # double-count on retries). None == a transient read failure
                # (e.g. sqlite lock from the concurrent manager daemon): keep
                # the prior accumulator rather than overwriting it, so a glitch
                # can't trip the terminal breaker on a healthy story. The
                # handler persisted its own state transition; persist again so
                # total_attempts/total_spend_usd survive to the next tick's
                # pre-dispatch breaker check.
                _spend = _story_ledger_spend_usd(db, story.id)
                if _spend is not None:
                    story.total_spend_usd = _spend
                # WS1.1 advance-decay: if this dispatch moved the story to a NEW
                # happy-path milestone (strictly beyond its high-water mark, so
                # NOT a dev<->review oscillation), reset the attempt counter so a
                # poisoned historical count (e.g. attempts burned by an earlier
                # infra bug) can't false-trip the breaker on a story that IS
                # progressing. Spend stays the hard ceiling. Persist happens
                # below so the decayed counter survives to the next tick.
                if _apply_advance_decay(story):
                    log_story_event(
                        story.id,
                        "budget_attempts_decayed",
                        {
                            "to_state": story.state,
                            "progress_ordinal": story.max_progress_ordinal,
                            "total_spend_usd": round(story.total_spend_usd, 4),
                        },
                        software_factory_root=root,
                        slug_hint=story.slug,
                    )
                H.persist_story(story, db)
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
                # WS4.2: append a typed, replayable chain_step record for this
                # dispatch (append-only per-app stream; content hash/ref of the
                # step's persisted artifact). Best-effort — never crashes a tick.
                emit_chain_step(
                    story,
                    handler=handler_name,
                    from_state=from_state,
                    to_state=story.state,
                    outcome="error" if result.error else "advanced",
                    software_factory_root=root,
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
                    # EXTEND, never overwrite: the start-of-tick pass may have
                    # already recorded decisions (and advanced those stories),
                    # so a quiet end-of-tick pass would otherwise erase them.
                    # Dedup by pr_number: in dry-run a "merged" fixture isn't
                    # actually removed, so both passes report the same PR —
                    # keep the first decision per PR.
                    _seen_prs = {m.pr_number for m in (summary.merges or [])}
                    summary.merges = list(summary.merges or []) + [
                        m for m in merge_actions if m.pr_number not in _seen_prs
                    ]
                except Exception as exc:
                    # Auto-merge failures must not break the tick — the
                    # operator can still inspect the chain via ``factory
                    # story`` and re-run auto-merge by hand.
                    summary.errors.append(("auto-merge", repr(exc)))

        # Post-merge main-branch CI-health monitor (D004). Cheap (1-2 ``gh``
        # calls) — runs once per app per tick regardless of in-flight story
        # count, since it isn't watching any particular story: it's watching
        # main itself for a required check that went red AFTER merge (flaky
        # test, merge-interaction failure, infra/runner change). Gated the
        # same way as auto-merge: an explicit settings flag, and suppressed
        # in modes where forward motion (here: filing a new direction) is
        # deliberately paused.
        if settings.ci_health.enabled:
            current_mode = get_mode(root, db_path=db)
            if current_mode not in {"paused", "drain-reviews"}:
                try:
                    summary.ci_health = main_ci_health_tick(root, app, dry_run=dry_run)
                except Exception as exc:
                    # Best-effort: a CI-health monitor failure must never
                    # break the tick.
                    summary.errors.append(("ci-health", repr(exc)))

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
        "skipped": summary.skipped,
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
        "ci_health": (
            None
            if summary.ci_health is None
            else {
                "state": summary.ci_health.state,
                "filed": summary.ci_health.filed,
                "filed_direction_id": summary.ci_health.filed_direction_id,
                "reason": summary.ci_health.reason,
                "required_failing": list(summary.ci_health.required_failing),
                "advisory_failing": list(summary.ci_health.advisory_failing),
            }
        ),
    }
