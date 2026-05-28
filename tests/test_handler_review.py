"""Tests for ``factory.chain.handlers.handle_review`` — verdict, slop bounce."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.app_config import AppConfig
from factory.chain.handlers import handle_review, persist_story
from factory.chain.state_machine import StoryRecord, StoryState


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(name="sacrifice", repo="x/y")


def _story_at_tests_green(root: Path) -> StoryRecord:
    db = root / "state" / "factory.db"
    return persist_story(
        StoryRecord(
            direction_id="002",
            app="sacrifice",
            title="t",
            slug="t",
            scope="backend",
            state=StoryState.TESTS_GREEN.value,
        ),
        db,
    )


def test_high_quality_approve_advances_to_reviewer_done(
    temp_root: Path, app_config: AppConfig
) -> None:
    s = _story_at_tests_green(temp_root)
    db = temp_root / "state" / "factory.db"
    fixture = {
        "verdict": "approve",
        "findings": [],
        "test_quality_score": 0.95,
        "test_quality_findings": [],
        "comments_to_post": [],
        "summary": "approve",
    }
    result = handle_review(s, app_config, temp_root, dry_run=True, db_path=db, fixture=fixture)
    assert result.next_state == StoryState.REVIEWER_DONE
    assert s.state == StoryState.REVIEWER_DONE.value


def test_low_test_quality_score_bounces_to_request_changes(
    temp_root: Path, app_config: AppConfig
) -> None:
    """Score below 0.7 triggers REVIEWER_REQUESTED_CHANGES even if verdict=approve."""
    s = _story_at_tests_green(temp_root)
    db = temp_root / "state" / "factory.db"
    fixture = {
        "verdict": "approve",
        "findings": [],
        "test_quality_score": 0.42,
        "test_quality_findings": [
            {
                "test_name": "test_x",
                "issue": "asserts on value set on previous line",
                "fix_suggestion": "assert against the real subject's output",
            }
        ],
        "comments_to_post": [],
        "summary": "slop tests",
    }
    result = handle_review(s, app_config, temp_root, dry_run=True, db_path=db, fixture=fixture)
    assert result.next_state == StoryState.REVIEWER_REQUESTED_CHANGES
    # Reviewer persisted the JSON for later inspection.
    assert s.reviewer_result_json is not None
    assert "0.42" in s.reviewer_result_json or "0.4" in s.reviewer_result_json


def test_request_changes_due_to_findings(temp_root: Path, app_config: AppConfig) -> None:
    """High-severity findings flip an otherwise-approve to request_changes."""
    s = _story_at_tests_green(temp_root)
    db = temp_root / "state" / "factory.db"
    fixture = {
        "verdict": "request_changes",
        "findings": [
            {
                "severity": "high",
                "location": "src/x.py:42",
                "what": "SQLi",
                "fix_suggestion": "use param binding",
            }
        ],
        "test_quality_score": 0.85,
        "test_quality_findings": [],
        "comments_to_post": [],
        "summary": "security issue",
    }
    result = handle_review(s, app_config, temp_root, dry_run=True, db_path=db, fixture=fixture)
    assert result.next_state == StoryState.REVIEWER_REQUESTED_CHANGES


# --------------------------------------------------------------------------- #
# Hard convergence guard — non-converging dev<->reviewer loops are capped at
# _MAX_REVIEW_CYCLES request-changes verdicts and routed to a terminal blocked
# state instead of looping back to dev indefinitely.
# --------------------------------------------------------------------------- #

_REQUEST_CHANGES_FIXTURE = {
    "verdict": "request_changes",
    "findings": [
        {
            "severity": "high",
            "location": "src/x.py:42",
            "what": "still not addressed",
            "fix_suggestion": "fix it",
        }
    ],
    "test_quality_score": 0.85,
    "test_quality_findings": [],
    "comments_to_post": [],
    "summary": "more changes",
}


def _story_at_tests_green_with_cycles(root: Path, cycles: int) -> StoryRecord:
    db = root / "state" / "factory.db"
    return persist_story(
        StoryRecord(
            direction_id="002",
            app="sacrifice",
            title="t",
            slug="t",
            scope="backend",
            state=StoryState.TESTS_GREEN.value,
            reviewer_cycles=cycles,
        ),
        db,
    )


def test_request_changes_increments_reviewer_cycles(
    temp_root: Path, app_config: AppConfig
) -> None:
    s = _story_at_tests_green_with_cycles(temp_root, 0)
    db = temp_root / "state" / "factory.db"
    handle_review(
        s, app_config, temp_root, dry_run=True, db_path=db,
        fixture=_REQUEST_CHANGES_FIXTURE,
    )
    assert s.reviewer_cycles == 1
    assert s.state == StoryState.REVIEWER_REQUESTED_CHANGES.value


def test_guard_does_not_fire_below_max(temp_root: Path, app_config: AppConfig) -> None:
    """At cycle 2 (below the cap of 3) the story still loops back to dev."""
    s = _story_at_tests_green_with_cycles(temp_root, 1)
    db = temp_root / "state" / "factory.db"
    result = handle_review(
        s, app_config, temp_root, dry_run=True, db_path=db,
        fixture=_REQUEST_CHANGES_FIXTURE,
    )
    assert s.reviewer_cycles == 2
    assert result.next_state == StoryState.REVIEWER_REQUESTED_CHANGES


def test_guard_blocks_at_max_cycles(temp_root: Path, app_config: AppConfig) -> None:
    """The 3rd request-changes verdict routes to the terminal blocked state."""
    s = _story_at_tests_green_with_cycles(temp_root, 2)
    db = temp_root / "state" / "factory.db"
    result = handle_review(
        s, app_config, temp_root, dry_run=True, db_path=db,
        fixture=_REQUEST_CHANGES_FIXTURE,
    )
    assert s.reviewer_cycles == 3
    assert result.next_state == StoryState.BLOCKED_REVIEW_NONCONVERGENT
    assert s.state == StoryState.BLOCKED_REVIEW_NONCONVERGENT.value
    assert s.error is not None and "did not converge" in s.error


def test_approve_never_triggers_guard(temp_root: Path, app_config: AppConfig) -> None:
    """A clean approve advances normally even if prior cycles were high."""
    s = _story_at_tests_green_with_cycles(temp_root, 2)
    db = temp_root / "state" / "factory.db"
    fixture = {
        "verdict": "approve",
        "findings": [],
        "test_quality_score": 0.95,
        "test_quality_findings": [],
        "comments_to_post": [],
        "summary": "approve",
    }
    result = handle_review(s, app_config, temp_root, dry_run=True, db_path=db, fixture=fixture)
    assert result.next_state == StoryState.REVIEWER_DONE
    # Approve does not increment the request-changes counter.
    assert s.reviewer_cycles == 2


def test_blocked_review_nonconvergent_is_terminal() -> None:
    """The guard's target state must have no outgoing transitions."""
    from factory.chain.state_machine import is_terminal

    assert is_terminal(StoryState.BLOCKED_REVIEW_NONCONVERGENT)
