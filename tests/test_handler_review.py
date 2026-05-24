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
