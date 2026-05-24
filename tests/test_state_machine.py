"""Tests for ``factory.chain.state_machine``.

The state machine is the spine of the TDD chain. Wrong transitions = work
gets stuck or skips a step. These tests assert the happy path AND every
slop/retry/error edge.
"""

from __future__ import annotations

import pytest

from factory.chain.state_machine import (
    EVENT_DEV_EXHAUSTED,
    EVENT_DEV_STARTED,
    EVENT_DEV_TESTS_GREEN,
    EVENT_DEV_TESTS_RED,
    EVENT_DOCS_ENFORCER_CHECK,
    EVENT_DOCS_ENFORCER_FAIL,
    EVENT_DOCS_ENFORCER_PASS,
    EVENT_REVIEWER_APPROVE,
    EVENT_REVIEWER_REQUEST_CHANGES,
    EVENT_REVIEWER_STARTED,
    EVENT_SM_DONE,
    EVENT_SM_STARTED,
    EVENT_TECH_WRITER_DONE,
    EVENT_TECH_WRITER_STARTED,
    EVENT_TEST_DESIGN_DONE,
    EVENT_TEST_DESIGN_STARTED,
    EVENT_TEST_IMPL_SLOP,
    EVENT_TEST_IMPL_STARTED,
    EVENT_TESTS_RED,
    IllegalTransitionError,
    StoryRecord,
    StoryState,
    advance,
    list_transitions_from,
)


def _story(state: StoryState = StoryState.STORY_CREATED) -> StoryRecord:
    """Build a minimal StoryRecord for state-machine testing."""
    return StoryRecord(
        direction_id="002",
        app="sacrifice",
        title="t",
        slug="t",
        scope="backend",
        state=state.value,
    )


def test_happy_path_advances_through_every_state() -> None:
    """Verify the whole chain happy-path: created -> ... -> pr_open."""
    s = _story()
    # SM runs before test_design.
    s.state = advance(s, EVENT_SM_STARTED).value
    assert s.state == StoryState.SM_IN_PROGRESS.value
    s.state = advance(s, EVENT_SM_DONE).value
    assert s.state == StoryState.SM_DONE.value
    s.state = advance(s, EVENT_TEST_DESIGN_STARTED).value
    assert s.state == StoryState.TEST_DESIGN_IN_PROGRESS.value
    s.state = advance(s, EVENT_TEST_DESIGN_DONE).value
    assert s.state == StoryState.TEST_DESIGN_DONE.value
    s.state = advance(s, EVENT_TEST_IMPL_STARTED).value
    assert s.state == StoryState.TEST_IMPLEMENTATION_IN_PROGRESS.value
    s.state = advance(s, EVENT_TESTS_RED).value
    assert s.state == StoryState.TESTS_RED.value
    s.state = advance(s, EVENT_DEV_STARTED).value
    assert s.state == StoryState.DEV_IN_PROGRESS.value
    s.state = advance(s, EVENT_DEV_TESTS_GREEN).value
    assert s.state == StoryState.TESTS_GREEN.value
    s.state = advance(s, EVENT_REVIEWER_STARTED).value
    assert s.state == StoryState.REVIEWER_IN_PROGRESS.value
    s.state = advance(s, EVENT_REVIEWER_APPROVE).value
    assert s.state == StoryState.REVIEWER_DONE.value
    s.state = advance(s, EVENT_TECH_WRITER_STARTED).value
    assert s.state == StoryState.TECH_WRITER_IN_PROGRESS.value
    s.state = advance(s, EVENT_TECH_WRITER_DONE).value
    assert s.state == StoryState.TECH_WRITER_DONE.value
    s.state = advance(s, EVENT_DOCS_ENFORCER_CHECK).value
    assert s.state == StoryState.DOCS_ENFORCER_CHECK.value
    s.state = advance(s, EVENT_DOCS_ENFORCER_PASS).value
    assert s.state == StoryState.PR_OPEN.value


def test_tests_slop_bounces_to_blocked() -> None:
    """Test-Implementer slop signal lands us in BLOCKED_TESTS_NEED_CLARIFICATION,
    which is where the chain files a (tests-need-clarification) direction."""
    s = _story(StoryState.TEST_IMPLEMENTATION_IN_PROGRESS)
    next_state = advance(s, EVENT_TEST_IMPL_SLOP)
    assert next_state == StoryState.BLOCKED_TESTS_NEED_CLARIFICATION


def test_dev_retry_loops_then_exhausts() -> None:
    """Dev failure path: DEV_IN_PROGRESS -> DEV_RETRY -> DEV_IN_PROGRESS until exhausted."""
    s = _story(StoryState.DEV_IN_PROGRESS)
    # Tests still red after a dev run.
    s.state = advance(s, EVENT_DEV_TESTS_RED).value
    assert s.state == StoryState.DEV_RETRY.value
    # Try again.
    s.state = advance(s, EVENT_DEV_STARTED).value
    assert s.state == StoryState.DEV_IN_PROGRESS.value
    # And again red.
    s.state = advance(s, EVENT_DEV_TESTS_RED).value
    assert s.state == StoryState.DEV_RETRY.value
    # After max retries, the handler emits EVENT_DEV_EXHAUSTED.
    s.state = advance(s, EVENT_DEV_EXHAUSTED).value
    assert s.state == StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value


def test_reviewer_request_changes_loops_back_to_dev() -> None:
    s = _story(StoryState.REVIEWER_IN_PROGRESS)
    s.state = advance(s, EVENT_REVIEWER_REQUEST_CHANGES).value
    assert s.state == StoryState.REVIEWER_REQUESTED_CHANGES.value
    s.state = advance(s, EVENT_DEV_STARTED).value
    assert s.state == StoryState.DEV_IN_PROGRESS.value


def test_docs_enforcer_fail_loops_back_for_fix() -> None:
    s = _story(StoryState.DOCS_ENFORCER_CHECK)
    s.state = advance(s, EVENT_DOCS_ENFORCER_FAIL).value
    assert s.state == StoryState.REVIEWER_REQUESTED_CHANGES.value


def test_illegal_transition_raises() -> None:
    """An event not in the transition table for the current state raises."""
    s = _story(StoryState.STORY_CREATED)
    with pytest.raises(IllegalTransitionError):
        advance(s, EVENT_DEV_TESTS_GREEN)


def test_list_transitions_from_story_created() -> None:
    """STORY_CREATED has exactly one outgoing edge: EVENT_SM_STARTED."""
    edges = list_transitions_from(StoryState.STORY_CREATED)
    assert edges == [(EVENT_SM_STARTED, StoryState.SM_IN_PROGRESS)]


def test_sm_in_progress_transitions_to_sm_done() -> None:
    s = _story(StoryState.SM_IN_PROGRESS)
    assert advance(s, EVENT_SM_DONE) == StoryState.SM_DONE


def test_sm_done_transitions_to_test_design_in_progress() -> None:
    s = _story(StoryState.SM_DONE)
    assert advance(s, EVENT_TEST_DESIGN_STARTED) == StoryState.TEST_DESIGN_IN_PROGRESS


def test_blocked_state_has_no_outgoing_transitions() -> None:
    """BLOCKED is terminal for the chain — only human intervention unsticks it."""
    edges = list_transitions_from(StoryState.BLOCKED_TESTS_NEED_CLARIFICATION)
    assert edges == []


def test_advance_does_not_mutate_story() -> None:
    """``advance`` is pure — the caller is responsible for persisting the new state."""
    s = _story(StoryState.STORY_CREATED)
    next_state = advance(s, EVENT_SM_STARTED)
    assert s.state == StoryState.STORY_CREATED.value  # unchanged
    assert next_state == StoryState.SM_IN_PROGRESS
