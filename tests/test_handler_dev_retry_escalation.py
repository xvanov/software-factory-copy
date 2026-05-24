"""Tests for ``factory.chain.handlers.handle_dev`` retry + escalation logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.app_config import AppConfig
from factory.chain.handlers import handle_dev, persist_story
from factory.chain.state_machine import StoryRecord, StoryState


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "apps" / "sacrifice").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(name="sacrifice", repo="x/y")


def _story_at_tests_red(root: Path) -> StoryRecord:
    db = root / "state" / "factory.db"
    return persist_story(
        StoryRecord(
            direction_id="002",
            app="sacrifice",
            title="t",
            slug="t",
            scope="backend",
            state=StoryState.TESTS_RED.value,
        ),
        db,
    )


def test_dry_run_happy_path_first_try_green(temp_root: Path, app_config: AppConfig) -> None:
    s = _story_at_tests_red(temp_root)
    db = temp_root / "state" / "factory.db"
    result = handle_dev(s, app_config, temp_root, dry_run=True, db_path=db)
    assert result.next_state == StoryState.TESTS_GREEN
    assert s.dev_retries == 0
    assert s.current_model_tier == "standard"


def test_first_failure_increments_retry_and_escalates_tier(
    temp_root: Path, app_config: AppConfig
) -> None:
    s = _story_at_tests_red(temp_root)
    db = temp_root / "state" / "factory.db"
    result = handle_dev(s, app_config, temp_root, dry_run=True, db_path=db, force_red=True)

    assert result.next_state == StoryState.DEV_RETRY
    assert s.dev_retries == 1
    # First failure must escalate standard -> hard.
    assert s.current_model_tier == "hard"


def test_three_failures_exhaust_to_blocked(temp_root: Path, app_config: AppConfig) -> None:
    """After 3 failures, the chain BLOCKs the story for human review."""
    s = _story_at_tests_red(temp_root)
    db = temp_root / "state" / "factory.db"

    # Failure 1: TESTS_RED -> DEV_IN_PROGRESS -> DEV_RETRY (retries=1, tier=hard).
    result1 = handle_dev(s, app_config, temp_root, dry_run=True, db_path=db, force_red=True)
    assert result1.next_state == StoryState.DEV_RETRY
    assert s.dev_retries == 1

    # Failure 2: DEV_RETRY -> DEV_IN_PROGRESS -> DEV_RETRY (retries=2).
    result2 = handle_dev(s, app_config, temp_root, dry_run=True, db_path=db, force_red=True)
    assert result2.next_state == StoryState.DEV_RETRY
    assert s.dev_retries == 2

    # Failure 3: at retries=3 the handler emits EVENT_DEV_EXHAUSTED ->
    # BLOCKED_TESTS_NEED_CLARIFICATION.
    result3 = handle_dev(s, app_config, temp_root, dry_run=True, db_path=db, force_red=True)
    assert result3.next_state == StoryState.BLOCKED_TESTS_NEED_CLARIFICATION
    assert s.dev_retries == 3
    assert s.error and "exhausted" in s.error
    assert result3.error and "exhausted" in result3.error
