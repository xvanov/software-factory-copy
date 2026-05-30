"""Tests for ``factory.chain.handlers.handle_review`` — verdict, slop bounce."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.app_config import AppConfig
from factory.chain.handlers import _MAX_REVIEW_STUCK, handle_review, persist_story
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


def test_low_test_quality_score_routes_to_test_loop(
    temp_root: Path, app_config: AppConfig
) -> None:
    """Score below 0.7 routes to the TEST loop (test_implementer rewrites the
    tests), NOT to dev — dev cannot edit frozen test files."""
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
    assert result.next_state == StoryState.TEST_DESIGN_DONE
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


def test_guard_blocks_on_repeated_identical_findings(
    temp_root: Path, app_config: AppConfig
) -> None:
    """Convergence is stability-based: the SAME findings 3 cycles in a row is
    genuine churn → terminal block. (Replaces the old raw-count cap.)"""
    s = _story_at_tests_green_with_cycles(temp_root, 0)
    db = temp_root / "state" / "factory.db"
    last = None
    for _ in range(_MAX_REVIEW_STUCK):
        s.state = StoryState.TESTS_GREEN.value  # chain re-dispatches reviewer
        persist_story(s, db)
        last = handle_review(
            s, app_config, temp_root, dry_run=True, db_path=db,
            fixture=_REQUEST_CHANGES_FIXTURE,  # identical findings every cycle
        )
    assert last is not None
    assert last.next_state == StoryState.BLOCKED_REVIEW_NONCONVERGENT
    assert s.error is not None and "stuck" in s.error


def test_guard_does_not_block_when_findings_change(
    temp_root: Path, app_config: AppConfig
) -> None:
    """DIFFERENT findings each cycle = progress, not churn → keeps routing,
    never blocks (until the hard backstop). This is the story-15 fix: mixed
    code+test findings get the cycles they need to converge."""
    s = _story_at_tests_green_with_cycles(temp_root, 0)
    db = temp_root / "state" / "factory.db"
    for i in range(_MAX_REVIEW_STUCK + 1):
        s.state = StoryState.TESTS_GREEN.value
        persist_story(s, db)
        fixture = {
            "verdict": "request_changes",
            "findings": [{"severity": "high", "location": "src/x.py:42",
                          "what": f"distinct issue #{i}", "fix_suggestion": "fix"}],
            "test_quality_score": 0.85,
            "test_quality_findings": [],
            "comments_to_post": [],
            "summary": "changes",
        }
        result = handle_review(s, app_config, temp_root, dry_run=True, db_path=db, fixture=fixture)
        # Never blocks on progress (each cycle's findings differ).
        assert result.next_state == StoryState.REVIEWER_REQUESTED_CHANGES


def test_test_only_findings_route_to_test_loop_even_with_high_score(
    temp_root: Path, app_config: AppConfig
) -> None:
    """Findings that point only at test files route to the test loop even when
    the reviewer reports a healthy test_quality_score (the story-15 root cause:
    dev cannot edit test files)."""
    s = _story_at_tests_green_with_cycles(temp_root, 0)
    db = temp_root / "state" / "factory.db"
    fixture = {
        "verdict": "request_changes",
        "findings": [
            {"severity": "high", "location": "backend/tests/test_uploads.py:10",
             "what": "tests do not cover SHA-256 behavior", "fix_suggestion": "add coverage"},
            {"severity": "low", "location": "tests/conftest.py:5",
             "what": "suite builds its own engine", "fix_suggestion": "reuse fixture"},
        ],
        "test_quality_score": 0.9,  # reviewer wrongly reports healthy score
        "test_quality_findings": [],
        "comments_to_post": [],
        "summary": "test issues",
    }
    result = handle_review(s, app_config, temp_root, dry_run=True, db_path=db, fixture=fixture)
    assert result.next_state == StoryState.TEST_DESIGN_DONE


def test_request_changes_low_score_routes_to_test_loop(
    temp_root: Path, app_config: AppConfig
) -> None:
    """request_changes with test_quality_score < 0.7 → test loop, not dev."""
    s = _story_at_tests_green_with_cycles(temp_root, 0)
    db = temp_root / "state" / "factory.db"
    fixture = {
        "verdict": "request_changes",
        "findings": [
            {"severity": "medium", "location": "t.test.ts:3",
             "what": "401 test asserts only substring", "fix_suggestion": "assert status"}
        ],
        "test_quality_score": 0.55,
        "test_quality_findings": [
            {"test_name": "test_401", "issue": "sloppy assertion",
             "fix_suggestion": "assert preserved status"}
        ],
        "comments_to_post": [],
        "summary": "tests are weak",
    }
    result = handle_review(s, app_config, temp_root, dry_run=True, db_path=db, fixture=fixture)
    assert result.next_state == StoryState.TEST_DESIGN_DONE
    assert s.reviewer_cycles == 1  # still counts toward the convergence guard


def test_request_changes_high_score_routes_to_dev(
    temp_root: Path, app_config: AppConfig
) -> None:
    """request_changes with a healthy test score → dev (code fix), not test loop."""
    s = _story_at_tests_green_with_cycles(temp_root, 0)
    db = temp_root / "state" / "factory.db"
    result = handle_review(
        s, app_config, temp_root, dry_run=True, db_path=db,
        fixture=_REQUEST_CHANGES_FIXTURE,  # test_quality_score 0.85
    )
    assert result.next_state == StoryState.REVIEWER_REQUESTED_CHANGES


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
