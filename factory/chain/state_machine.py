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
    """Every state a story can be in.

    Two chain variants share the same enum; the orchestrator picks the
    starting handler based on ``story.chain_kind``:

    * ``tdd`` (default): SM → test_design → test_impl → dev → reviewer →
      tech_writer → docs_enforcer → PR_OPEN. The historical pipeline.
    * ``docs``: docs_sm → docs_onboarder → docs_enforcer → PR_OPEN. Used for
      stories whose deliverable is canonical documentation (e.g. the initial
      ``context/`` bootstrap). Skips the TDD red→green loop because there is
      no executable code to drive against tests.

    Both variants converge at ``DOCS_ENFORCER_CHECK`` and onward; the
    enforcer + PR + deploy states are shared.
    """

    STORY_CREATED = "story_created"
    SM_IN_PROGRESS = "sm_in_progress"
    SM_DONE = "sm_done"
    TEST_DESIGN_IN_PROGRESS = "test_design_in_progress"
    TEST_DESIGN_DONE = "test_design_done"
    TEST_IMPLEMENTATION_IN_PROGRESS = "test_implementation_in_progress"
    TESTS_RED = "tests_red"
    # Item 4: between TESTS_RED and DEV_IN_PROGRESS we run a one-shot
    # harness precheck: pytest must COLLECT (exit 0 or 1) before dev
    # gets dispatched. Collection failure (exit 2 or 3) means the test
    # set is environmentally broken — missing .env, missing dep,
    # ImportError in conftest — and dev cannot fix it. The precheck
    # routes the story to ``BLOCKED_TESTS_NEED_CLARIFICATION`` with a
    # ``factory_needs_redesign`` event so the improver/operator sees the
    # signal instead of dev burning the retry budget on a config bug.
    HARNESS_PRECHECK_IN_PROGRESS = "harness_precheck_in_progress"
    DEV_IN_PROGRESS = "dev_in_progress"
    DEV_RETRY = "dev_retry"
    TESTS_GREEN = "tests_green"
    REVIEWER_IN_PROGRESS = "reviewer_in_progress"
    REVIEWER_DONE = "reviewer_done"
    REVIEWER_REQUESTED_CHANGES = "reviewer_requested_changes"
    TECH_WRITER_IN_PROGRESS = "tech_writer_in_progress"
    TECH_WRITER_DONE = "tech_writer_done"
    # Docs chain: lightweight path for documentation-only stories. Onboarder
    # writes the canonical files in one shot; no test loop.
    DOCS_SM_IN_PROGRESS = "docs_sm_in_progress"
    DOCS_SM_DONE = "docs_sm_done"
    DOCS_ONBOARDER_IN_PROGRESS = "docs_onboarder_in_progress"
    DOCS_ONBOARDER_DONE = "docs_onboarder_done"
    DOCS_ENFORCER_CHECK = "docs_enforcer_check"
    PR_OPEN = "pr_open"
    CI_PENDING = "ci_pending"
    CI_GREEN = "ci_green"
    READY_FOR_MERGE = "ready_for_merge"
    DEPLOY_PENDING = "deploy_pending"
    DEPLOYED = "deployed"
    BLOCKED_TESTS_NEED_CLARIFICATION = "blocked_tests_need_clarification"
    BLOCKED_DEPLOY_FAILED = "blocked_deploy_failed"
    # Hard convergence guard: when the dev<->reviewer loop fails to converge
    # within _MAX_REVIEW_CYCLES reviewer passes, the reviewer handler routes
    # here instead of bouncing back to REVIEWER_REQUESTED_CHANGES. Terminal
    # (no outgoing transition) so the orchestrator stops dispatching the
    # story and it surfaces for human review instead of looping indefinitely.
    BLOCKED_REVIEW_NONCONVERGENT = "blocked_review_nonconvergent"


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
    # Which chain variant drives this story. ``tdd`` is the historical
    # default; ``docs`` routes through docs_sm → docs_onboarder →
    # docs_enforcer for documentation-only deliverables. The orchestrator
    # reads this when dispatching out of STORY_CREATED.
    chain_kind: str = Field(default="tdd", index=True)
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
    # JSON-serialised list of prior dev attempts on this story. Each entry:
    # ``{"attempt": N, "ts": "...", "test_output_tail": "...",
    #    "files_touched": [...], "summary": "..."}``. Carried forward into
    # the next dev sandbox's initial message so the LLM sees what it tried
    # and what failed instead of re-discovering dead ends from scratch.
    dev_attempts_json: str | None = None
    # Hard convergence guard counter: incremented each time the reviewer
    # returns a request-changes verdict in handle_review. When it reaches
    # ``_MAX_REVIEW_CYCLES`` the story is routed to
    # ``BLOCKED_REVIEW_NONCONVERGENT`` instead of looping back to dev, so a
    # non-converging dev<->reviewer ping-pong cannot burn budget unbounded.
    reviewer_cycles: int = 0
    current_model_tier: str = "standard"  # standard | hard
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    error: str | None = None
    # Phase 3: last cap/mode rejection reason emitted by the dispatcher.
    last_rejection_reason: str | None = None
    # Phase 8 cleanup: per-gate recorded outcomes the dev/CI handler writes
    # after running each tool. Dry-run gates read these instead of returning
    # an unconditional pass — None means "not run yet" and is treated as a
    # blocking missing-signal.
    lint_passed: bool | None = None
    format_passed: bool | None = None
    types_passed: bool | None = None
    coverage_passed: bool | None = None
    # Item 4 — harness precheck. Set True after a one-shot pytest
    # collect+exit pass succeeds against the per-story worktree (with
    # ONLY the test files committed, before dev writes production
    # code). The orchestrator's dispatch table reads this flag when
    # state==TESTS_RED to decide whether to fire ``harness_precheck``
    # or skip ahead to ``dev``. Default ``False`` so existing stories
    # transparently get the precheck on their next visit.
    harness_precheck_passed: bool = False
    # Phase 3 EBS (Evidence-Based Scheduling). PM assigns Fibonacci
    # story points at split time (1, 2, 3, 5, 8, 13). The chain
    # computes ``estimated_seconds`` from per-(persona, points)
    # baselines at story-creation time; the Monte Carlo simulator
    # uses points + persona velocities to project per-direction
    # ETAs. Both columns are nullable — legacy stories spawned
    # before this column existed keep ``None``.
    points: int | None = None
    estimated_seconds: float | None = None


# Event names — strings the chain emits when a handler completes.
EVENT_SM_STARTED = "sm_started"
EVENT_SM_DONE = "sm_done"
EVENT_TEST_DESIGN_STARTED = "test_design_started"
EVENT_TEST_DESIGN_DONE = "test_design_done"
EVENT_TEST_IMPL_STARTED = "test_impl_started"
EVENT_TESTS_RED = "tests_red"
EVENT_TEST_IMPL_SLOP = "test_impl_slop"
# Test-Implementer slop (tests passed before any implementation existed) but
# under the retry cap: route BACK to the test loop (test_implementer rewrites
# the tests with explicit slop feedback) instead of terminally blocking. Caps
# at _MAX_TEST_IMPL_SLOP_RETRIES per the "nothing loops >3" rule; the EVENT_
# TEST_IMPL_SLOP edge above is the terminal block taken once the cap is hit.
EVENT_TEST_IMPL_SLOP_RETRY = "test_impl_slop_retry"
# Re-plan: when re-running the Test-Implementer can't resolve slop/test-quality
# (cap hit), the defect is usually in the PLAN, not the implementation. Route
# back to the Test-Designer (via SM_DONE → test_design) for ONE re-plan with
# the failure as feedback, before blocking. Leverages the designer's
# contract-grounding/scope/e2e-gating rules to fix the plan at the source.
EVENT_TEST_IMPL_REPLAN = "test_impl_replan"
# Item 4 — harness precheck events. Started fires on the
# TESTS_RED→HARNESS_PRECHECK_IN_PROGRESS edge; PASS routes to DEV_IN_PROGRESS;
# FAIL routes to BLOCKED_TESTS_NEED_CLARIFICATION (same terminal blocked
# state the test-impl-slop path lands in — it's the operator-attention
# bucket).
EVENT_HARNESS_PRECHECK_STARTED = "harness_precheck_started"
EVENT_HARNESS_PRECHECK_PASS = "harness_precheck_pass"
EVENT_HARNESS_PRECHECK_FAIL = "harness_precheck_fail"
EVENT_DEV_STARTED = "dev_started"
EVENT_DEV_TESTS_GREEN = "dev_tests_green"
EVENT_DEV_TESTS_RED = "dev_tests_red"  # dev finished but tests still red
EVENT_DEV_EXHAUSTED = "dev_exhausted"  # max retries hit
EVENT_TESTS_NEED_CLARIFICATION = "tests_need_clarification"  # dev signalled bad tests
EVENT_REVIEWER_STARTED = "reviewer_started"
EVENT_REVIEWER_APPROVE = "reviewer_approve"
EVENT_REVIEWER_REQUEST_CHANGES = "reviewer_request_changes"
# Reviewer requested changes for the _MAX_REVIEW_CYCLES-th time without the
# story converging — the hard convergence guard fires this instead of
# EVENT_REVIEWER_REQUEST_CHANGES to break the dev<->reviewer ping-pong.
EVENT_REVIEW_NONCONVERGENT = "review_nonconvergent"
# Reviewer rejected primarily on TEST QUALITY (test_quality_score < threshold):
# the tests themselves are wrong/insufficient/misplaced. Route to the test
# loop (test_implementer rewrites the tests) rather than to dev, which is
# forbidden from editing test files and would only block trying.
EVENT_REVIEWER_TEST_QUALITY = "reviewer_test_quality"
EVENT_TECH_WRITER_STARTED = "tech_writer_started"
EVENT_TECH_WRITER_DONE = "tech_writer_done"
EVENT_DOCS_ENFORCER_CHECK = "docs_enforcer_check"
EVENT_DOCS_ENFORCER_PASS = "docs_enforcer_pass"
EVENT_DOCS_ENFORCER_FAIL = "docs_enforcer_fail"
# Docs chain events.
EVENT_DOCS_SM_STARTED = "docs_sm_started"
EVENT_DOCS_SM_DONE = "docs_sm_done"
EVENT_DOCS_ONBOARDER_STARTED = "docs_onboarder_started"
EVENT_DOCS_ONBOARDER_DONE = "docs_onboarder_done"
EVENT_DOCS_ONBOARDER_FAILED = "docs_onboarder_failed"
# Phase 5: post-merge deploy chain.
EVENT_MERGED = "merged"  # auto-merge or webhook flips READY_FOR_MERGE -> DEPLOY_PENDING.
EVENT_DEPLOY_STARTED = "deploy_started"
EVENT_DEPLOY_SUCCEEDED = "deploy_succeeded"
EVENT_DEPLOY_FAILED = "deploy_failed"
EVENT_DEPLOY_SKIPPED = "deploy_skipped"  # mode/cap rejection or deploy.enabled=false
# Auto-merge worker gave up: the PR is terminally un-mergeable (closed,
# already-merged out-of-band, or CONFLICTING/DIRTY). Routes the story to the
# terminal BLOCKED_DEPLOY_FAILED sink so it stops being retried every tick.
EVENT_PR_UNMERGEABLE = "pr_unmergeable"


# Lookup table: (current_state, event) -> next_state.
# This is the source of truth for the chain's transition graph. Any
# (state, event) pair not in this map is an illegal transition.
_TRANSITIONS: dict[tuple[StoryState, str], StoryState] = {
    # ---- TDD chain (chain_kind == "tdd") ----
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
    # Slop under the retry cap → back to the test loop to rewrite the tests.
    (
        StoryState.TEST_IMPLEMENTATION_IN_PROGRESS,
        EVENT_TEST_IMPL_SLOP_RETRY,
    ): StoryState.TEST_DESIGN_DONE,
    # Slop/test-quality un-resolvable by the implementer → re-plan via the
    # Test-Designer (SM_DONE dispatches test_design), once, before blocking.
    (
        StoryState.TEST_IMPLEMENTATION_IN_PROGRESS,
        EVENT_TEST_IMPL_REPLAN,
    ): StoryState.SM_DONE,
    # Item 4 — harness precheck between TESTS_RED and DEV. Runs ONCE
    # per story (the orchestrator's dispatch table reads
    # ``story.harness_precheck_passed`` and skips re-running on
    # subsequent visits to TESTS_RED). PASS routes back to TESTS_RED
    # with the flag set so dev gets dispatched next iteration; FAIL
    # routes to the operator-attention bucket.
    (
        StoryState.TESTS_RED,
        EVENT_HARNESS_PRECHECK_STARTED,
    ): StoryState.HARNESS_PRECHECK_IN_PROGRESS,
    (
        StoryState.HARNESS_PRECHECK_IN_PROGRESS,
        EVENT_HARNESS_PRECHECK_PASS,
    ): StoryState.TESTS_RED,
    (
        StoryState.HARNESS_PRECHECK_IN_PROGRESS,
        EVENT_HARNESS_PRECHECK_FAIL,
    ): StoryState.BLOCKED_TESTS_NEED_CLARIFICATION,
    (StoryState.TESTS_RED, EVENT_DEV_STARTED): StoryState.DEV_IN_PROGRESS,
    (StoryState.DEV_IN_PROGRESS, EVENT_DEV_TESTS_GREEN): StoryState.TESTS_GREEN,
    (StoryState.DEV_IN_PROGRESS, EVENT_DEV_TESTS_RED): StoryState.DEV_RETRY,
    (StoryState.DEV_IN_PROGRESS, EVENT_DEV_EXHAUSTED): StoryState.BLOCKED_TESTS_NEED_CLARIFICATION,
    # Dev raised the TESTS_NEED_CLARIFICATION: escape hatch — the
    # test_implementer's tests are wrong/contradictory. Route back to
    # test_implementer (via TEST_DESIGN_DONE) so it can rewrite them.
    # dev_retries is NOT bumped — the clarification path preserves the
    # retry budget for genuine code-level dev work.
    (
        StoryState.DEV_IN_PROGRESS,
        EVENT_TESTS_NEED_CLARIFICATION,
    ): StoryState.TEST_DESIGN_DONE,
    (StoryState.DEV_RETRY, EVENT_DEV_STARTED): StoryState.DEV_IN_PROGRESS,
    (StoryState.DEV_RETRY, EVENT_DEV_EXHAUSTED): StoryState.BLOCKED_TESTS_NEED_CLARIFICATION,
    (StoryState.TESTS_GREEN, EVENT_REVIEWER_STARTED): StoryState.REVIEWER_IN_PROGRESS,
    (StoryState.REVIEWER_IN_PROGRESS, EVENT_REVIEWER_APPROVE): StoryState.REVIEWER_DONE,
    (
        StoryState.REVIEWER_IN_PROGRESS,
        EVENT_REVIEWER_REQUEST_CHANGES,
    ): StoryState.REVIEWER_REQUESTED_CHANGES,
    # Hard convergence guard: the Nth (N=_MAX_REVIEW_CYCLES) consecutive
    # request-changes verdict routes to a terminal blocked state instead of
    # looping back to dev. No outgoing transition → orchestrator stops
    # dispatching; the story waits for a human.
    (
        StoryState.REVIEWER_IN_PROGRESS,
        EVENT_REVIEW_NONCONVERGENT,
    ): StoryState.BLOCKED_REVIEW_NONCONVERGENT,
    # Reviewer changes route back to dev (if code finding) or to the test
    # loop (if the rejection is test-quality driven). The handler decides
    # which by inspecting the verdict payload's test_quality_score.
    # Test-quality rejections go to TEST_DESIGN_DONE so test_implementer
    # rewrites the tests — dev may not edit test files and would only block.
    (
        StoryState.REVIEWER_IN_PROGRESS,
        EVENT_REVIEWER_TEST_QUALITY,
    ): StoryState.TEST_DESIGN_DONE,
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
    # ---- Docs chain (chain_kind == "docs") ----
    # Skips the TDD red→green loop. Onboarder produces canonical doc files
    # in one sandbox pass; the enforcer + PR open path is shared with TDD.
    (StoryState.STORY_CREATED, EVENT_DOCS_SM_STARTED): StoryState.DOCS_SM_IN_PROGRESS,
    (StoryState.DOCS_SM_IN_PROGRESS, EVENT_DOCS_SM_DONE): StoryState.DOCS_SM_DONE,
    (
        StoryState.DOCS_SM_DONE,
        EVENT_DOCS_ONBOARDER_STARTED,
    ): StoryState.DOCS_ONBOARDER_IN_PROGRESS,
    (
        StoryState.DOCS_ONBOARDER_IN_PROGRESS,
        EVENT_DOCS_ONBOARDER_DONE,
    ): StoryState.DOCS_ONBOARDER_DONE,
    # Onboarder failure (e.g. sandbox crash, no files produced) goes to the
    # same BLOCKED state the TDD chain uses for "humans must look at this".
    (
        StoryState.DOCS_ONBOARDER_IN_PROGRESS,
        EVENT_DOCS_ONBOARDER_FAILED,
    ): StoryState.BLOCKED_TESTS_NEED_CLARIFICATION,
    (
        StoryState.DOCS_ONBOARDER_DONE,
        EVENT_DOCS_ENFORCER_CHECK,
    ): StoryState.DOCS_ENFORCER_CHECK,
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
    # Auto-merge gave up on a terminally un-mergeable PR (closed/conflicting):
    # route the story to the blocked sink so the worker stops retrying it every
    # tick and the chain can reach DONE. Reachable from every mergeable state.
    (StoryState.PR_OPEN, EVENT_PR_UNMERGEABLE): StoryState.BLOCKED_DEPLOY_FAILED,
    (StoryState.CI_GREEN, EVENT_PR_UNMERGEABLE): StoryState.BLOCKED_DEPLOY_FAILED,
    (StoryState.READY_FOR_MERGE, EVENT_PR_UNMERGEABLE): StoryState.BLOCKED_DEPLOY_FAILED,
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
