"""TDD chain state machine.

The chain advances each ``StoryRecord`` through a fixed sequence of states.
Each transition is driven by an *event* (handler completion, webhook
arrival, retry timer) carrying an optional payload (handler result, GitHub
event body, error string).

This module is pure: ``advance(story, event, payload) -> StoryState`` only
computes the next state from the current state and the event. Handlers in
``factory/chain/handlers.py`` are responsible for the side effects
(LLM calls, GitHub API calls, file writes); they consume the next-state
from ``advance`` to know what to persist.

State names mirror the plan section "TDD chain (the heart of the
workflow)". The story-record fields capture everything a handler needs to
resume from any state (test_plan_json, dev_retries, current_model_tier,
etc).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlmodel import Field, SQLModel


class StoryState(StrEnum):
    """Every state the TDD chain can be in for a single story."""

    STORY_CREATED = "story_created"
    SM_IN_PROGRESS = "sm_in_progress"
    SM_DONE = "sm_done"
    TEST_DESIGN_IN_PROGRESS = "test_design_in_progress"
    TEST_DESIGN_DONE = "test_design_done"
    TEST_IMPLEMENTATION_IN_PROGRESS = "test_implementation_in_progress"
    TESTS_RED = "tests_red"
    DEV_IN_PROGRESS = "dev_in_progress"
    DEV_RETRY = "dev_retry"
    TESTS_GREEN = "tests_green"
    REVIEWER_IN_PROGRESS = "reviewer_in_progress"
    REVIEWER_DONE = "reviewer_done"
    REVIEWER_REQUESTED_CHANGES = "reviewer_requested_changes"
    TECH_WRITER_IN_PROGRESS = "tech_writer_in_progress"
    TECH_WRITER_DONE = "tech_writer_done"
    DOCS_ENFORCER_CHECK = "docs_enforcer_check"
    PR_OPEN = "pr_open"
    CI_PENDING = "ci_pending"
    CI_GREEN = "ci_green"
    READY_FOR_MERGE = "ready_for_merge"
    DEPLOY_PENDING = "deploy_pending"
    DEPLOYED = "deployed"
    BLOCKED_TESTS_NEED_CLARIFICATION = "blocked_tests_need_clarification"
    BLOCKED_DEPLOY_FAILED = "blocked_deploy_failed"


class StoryRecord(SQLModel, table=True):
    """Per-story chain record, persisted in ``state/factory.db.stories``.

    One row per child_story spawned from a Direction's PM result. Carries
    every handler input/output and enough audit data to resume the chain
    after a crash.
    """

    __tablename__ = "stories"

    id: int | None = Field(default=None, primary_key=True)
    direction_id: str = Field(index=True)
    app: str = Field(index=True)
    title: str
    slug: str
    scope: str  # frontend | backend | infra | test | docs
    state: str = Field(default=StoryState.STORY_CREATED.value, index=True)
    github_issue_number: int | None = None
    github_branch: str | None = None
    github_pr_number: int | None = None
    story_file_path: str = ""
    sm_result_json: str | None = None  # JSON-serialized SM persona output
    test_plan_json: str | None = None  # JSON-serialized Test-Designer output
    test_implementer_result_json: str | None = None
    reviewer_result_json: str | None = None
    tech_writer_result_json: str | None = None
    dev_retries: int = 0
    current_model_tier: str = "standard"  # standard | hard
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    error: str | None = None
    # Phase 3: last cap/mode rejection reason emitted by the dispatcher.
    last_rejection_reason: str | None = None


# Event names — strings the chain emits when a handler completes.
EVENT_SM_STARTED = "sm_started"
EVENT_SM_DONE = "sm_done"
EVENT_TEST_DESIGN_STARTED = "test_design_started"
EVENT_TEST_DESIGN_DONE = "test_design_done"
EVENT_TEST_IMPL_STARTED = "test_impl_started"
EVENT_TESTS_RED = "tests_red"
EVENT_TEST_IMPL_SLOP = "test_impl_slop"
EVENT_DEV_STARTED = "dev_started"
EVENT_DEV_TESTS_GREEN = "dev_tests_green"
EVENT_DEV_TESTS_RED = "dev_tests_red"  # dev finished but tests still red
EVENT_DEV_EXHAUSTED = "dev_exhausted"  # max retries hit
EVENT_REVIEWER_STARTED = "reviewer_started"
EVENT_REVIEWER_APPROVE = "reviewer_approve"
EVENT_REVIEWER_REQUEST_CHANGES = "reviewer_request_changes"
EVENT_TECH_WRITER_STARTED = "tech_writer_started"
EVENT_TECH_WRITER_DONE = "tech_writer_done"
EVENT_DOCS_ENFORCER_CHECK = "docs_enforcer_check"
EVENT_DOCS_ENFORCER_PASS = "docs_enforcer_pass"
EVENT_DOCS_ENFORCER_FAIL = "docs_enforcer_fail"
# Phase 5: post-merge deploy chain.
EVENT_MERGED = "merged"  # auto-merge or webhook flips READY_FOR_MERGE -> DEPLOY_PENDING.
EVENT_DEPLOY_STARTED = "deploy_started"
EVENT_DEPLOY_SUCCEEDED = "deploy_succeeded"
EVENT_DEPLOY_FAILED = "deploy_failed"
EVENT_DEPLOY_SKIPPED = "deploy_skipped"  # mode/cap rejection or deploy.enabled=false


# Lookup table: (current_state, event) -> next_state.
# This is the source of truth for the chain's transition graph. Any
# (state, event) pair not in this map is an illegal transition.
_TRANSITIONS: dict[tuple[StoryState, str], StoryState] = {
    (StoryState.STORY_CREATED, EVENT_SM_STARTED): StoryState.SM_IN_PROGRESS,
    (StoryState.SM_IN_PROGRESS, EVENT_SM_DONE): StoryState.SM_DONE,
    (StoryState.SM_DONE, EVENT_TEST_DESIGN_STARTED): StoryState.TEST_DESIGN_IN_PROGRESS,
    (StoryState.TEST_DESIGN_IN_PROGRESS, EVENT_TEST_DESIGN_DONE): StoryState.TEST_DESIGN_DONE,
    (
        StoryState.TEST_DESIGN_DONE,
        EVENT_TEST_IMPL_STARTED,
    ): StoryState.TEST_IMPLEMENTATION_IN_PROGRESS,
    (StoryState.TEST_IMPLEMENTATION_IN_PROGRESS, EVENT_TESTS_RED): StoryState.TESTS_RED,
    (
        StoryState.TEST_IMPLEMENTATION_IN_PROGRESS,
        EVENT_TEST_IMPL_SLOP,
    ): StoryState.BLOCKED_TESTS_NEED_CLARIFICATION,
    (StoryState.TESTS_RED, EVENT_DEV_STARTED): StoryState.DEV_IN_PROGRESS,
    (StoryState.DEV_IN_PROGRESS, EVENT_DEV_TESTS_GREEN): StoryState.TESTS_GREEN,
    (StoryState.DEV_IN_PROGRESS, EVENT_DEV_TESTS_RED): StoryState.DEV_RETRY,
    (StoryState.DEV_IN_PROGRESS, EVENT_DEV_EXHAUSTED): StoryState.BLOCKED_TESTS_NEED_CLARIFICATION,
    (StoryState.DEV_RETRY, EVENT_DEV_STARTED): StoryState.DEV_IN_PROGRESS,
    (StoryState.DEV_RETRY, EVENT_DEV_EXHAUSTED): StoryState.BLOCKED_TESTS_NEED_CLARIFICATION,
    (StoryState.TESTS_GREEN, EVENT_REVIEWER_STARTED): StoryState.REVIEWER_IN_PROGRESS,
    (StoryState.REVIEWER_IN_PROGRESS, EVENT_REVIEWER_APPROVE): StoryState.REVIEWER_DONE,
    (
        StoryState.REVIEWER_IN_PROGRESS,
        EVENT_REVIEWER_REQUEST_CHANGES,
    ): StoryState.REVIEWER_REQUESTED_CHANGES,
    # Reviewer changes route back to dev (if code finding) or to designer
    # (if test-quality finding). The handler decides which by inspecting
    # the verdict payload; both go through DEV_RETRY on the test-quality
    # branch as well, since the implementer rewrites tests on the
    # designer's revised plan.
    (StoryState.REVIEWER_REQUESTED_CHANGES, EVENT_DEV_STARTED): StoryState.DEV_IN_PROGRESS,
    (
        StoryState.REVIEWER_DONE,
        EVENT_TECH_WRITER_STARTED,
    ): StoryState.TECH_WRITER_IN_PROGRESS,
    (StoryState.TECH_WRITER_IN_PROGRESS, EVENT_TECH_WRITER_DONE): StoryState.TECH_WRITER_DONE,
    # Phase 3 cleanup: if tech_writer's apply_context_updates fails (e.g. the
    # writer tried to touch a forbidden path), bounce the story back through
    # reviewer_requested_changes so the dev loop can replay rather than
    # leaving the chain stuck mid-write.
    (
        StoryState.TECH_WRITER_IN_PROGRESS,
        EVENT_REVIEWER_REQUEST_CHANGES,
    ): StoryState.REVIEWER_REQUESTED_CHANGES,
    (
        StoryState.TECH_WRITER_DONE,
        EVENT_DOCS_ENFORCER_CHECK,
    ): StoryState.DOCS_ENFORCER_CHECK,
    (StoryState.DOCS_ENFORCER_CHECK, EVENT_DOCS_ENFORCER_PASS): StoryState.PR_OPEN,
    (
        StoryState.DOCS_ENFORCER_CHECK,
        EVENT_DOCS_ENFORCER_FAIL,
    ): StoryState.REVIEWER_REQUESTED_CHANGES,
    # Phase 5 — post-merge deploy. The auto-merge worker flips
    # READY_FOR_MERGE → DEPLOY_PENDING on successful merge (also reachable
    # from CI_GREEN and PR_OPEN since some chains skip the intermediate
    # READY_FOR_MERGE recording). DEPLOY_PENDING is the orchestrator's cue
    # to dispatch handle_deploy. EVENT_DEPLOY_SKIPPED handles
    # apps with ``deploy.enabled=false`` so the story reaches a terminal
    # state without staying in DEPLOY_PENDING forever.
    (StoryState.READY_FOR_MERGE, EVENT_MERGED): StoryState.DEPLOY_PENDING,
    (StoryState.CI_GREEN, EVENT_MERGED): StoryState.DEPLOY_PENDING,
    (StoryState.PR_OPEN, EVENT_MERGED): StoryState.DEPLOY_PENDING,
    (StoryState.DEPLOY_PENDING, EVENT_DEPLOY_STARTED): StoryState.DEPLOY_PENDING,
    (StoryState.DEPLOY_PENDING, EVENT_DEPLOY_SUCCEEDED): StoryState.DEPLOYED,
    (StoryState.DEPLOY_PENDING, EVENT_DEPLOY_FAILED): StoryState.BLOCKED_DEPLOY_FAILED,
    (StoryState.DEPLOY_PENDING, EVENT_DEPLOY_SKIPPED): StoryState.DEPLOYED,
}


class IllegalTransitionError(ValueError):
    """Raised when ``advance`` is called with a (state, event) pair not in
    the transition table."""


def advance(story: StoryRecord, event: str, payload: dict[str, Any] | None = None) -> StoryState:
    """Compute the next state for ``story`` given ``event`` (and optional ``payload``).

    Pure: this function does not mutate ``story``, does not touch the
    database, does not call any handler. The caller is responsible for
    persisting the new state. Returns the ``StoryState`` enum value.

    Special-case logic:
      * ``EVENT_DEV_TESTS_RED`` advances to ``DEV_RETRY``. The caller MUST
        also bump ``story.dev_retries`` and may bump ``current_model_tier``
        before invoking the dev handler again. When retries exhaust, the
        caller passes ``EVENT_DEV_EXHAUSTED`` instead.
    """
    current = StoryState(story.state)
    key = (current, event)
    if key not in _TRANSITIONS:
        raise IllegalTransitionError(f"No transition: state={current.value!r} event={event!r}")
    return _TRANSITIONS[key]


def is_terminal(state: StoryState) -> bool:
    """A state is terminal for the chain when no outgoing transitions exist."""
    return not any(s == state for (s, _) in _TRANSITIONS)


def list_transitions_from(state: StoryState) -> list[tuple[str, StoryState]]:
    """Return ``[(event, next_state)]`` for all transitions out of ``state``.

    Useful for the `factory story <id>` CLI and for tests.
    """
    out: list[tuple[str, StoryState]] = []
    for (s, ev), ns in _TRANSITIONS.items():
        if s == state:
            out.append((ev, ns))
    return out
