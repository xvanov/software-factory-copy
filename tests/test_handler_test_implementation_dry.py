"""Tests for ``factory.chain.handlers.handle_test_implementation`` in dry-run mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.app_config import AppConfig
from factory.chain.handlers import (
    handle_test_design,
    handle_test_implementation,
    persist_story,
)
from factory.chain.state_machine import StoryRecord, StoryState


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "apps" / "sacrifice").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(name="sacrifice", repo="x/y")


def _story_through_design(temp_root: Path, app_config: AppConfig) -> StoryRecord:
    """Build a story already past SM_DONE (so the test-design handler is the
    next valid transition)."""
    db = temp_root / "state" / "factory.db"
    # Write a stub story file so the real-run path of test_design would have
    # content to read — dry-run doesn't need it but keeps the fixture honest.
    stories_dir = temp_root / "apps" / "sacrifice" / "stories"
    stories_dir.mkdir(parents=True, exist_ok=True)
    (stories_dir / "0-add-healthz.md").write_text("# Story\n\n## Acceptance Criteria\n", "utf-8")
    s = persist_story(
        StoryRecord(
            direction_id="002",
            app="sacrifice",
            title="Add /healthz",
            slug="add-healthz",
            scope="backend",
            state=StoryState.SM_DONE.value,
            story_file_path="stories/0-add-healthz.md",
        ),
        db,
    )
    handle_test_design(s, app_config, temp_root, dry_run=True, db_path=db)
    return s


def test_dry_run_advances_to_tests_red(temp_root: Path, app_config: AppConfig) -> None:
    s = _story_through_design(temp_root, app_config)
    db = temp_root / "state" / "factory.db"
    result = handle_test_implementation(s, app_config, temp_root, dry_run=True, db_path=db)
    assert result.next_state == StoryState.TESTS_RED
    assert s.state == StoryState.TESTS_RED.value
    # The implementer's payload made it onto the story record.
    impl = json.loads(s.test_implementer_result_json or "{}")
    assert impl["exit_code"] == 1, "dry-run must record exit_code=1 (red is the desired outcome)"
    assert impl["slop_detected"] is False
    # files_written reflects the plan the designer produced.
    assert isinstance(impl["files_written"], list) and len(impl["files_written"]) >= 1


def test_dry_run_slop_detected_bounces_to_blocked(temp_root: Path, app_config: AppConfig) -> None:
    """If we hand-edit a test_plan to set up the slop signal path, the
    handler must transition to BLOCKED_TESTS_NEED_CLARIFICATION."""
    s = _story_through_design(temp_root, app_config)
    db = temp_root / "state" / "factory.db"

    # Patch the dry-run fixture by injecting a result where slop_detected=True.
    # The cleanest way is to monkeypatch _dry_run_test_implementation;
    # since we don't want pytest-mock as a dep, we patch the module symbol.
    import factory.chain.handlers as H

    original = H._dry_run_test_implementation
    H._dry_run_test_implementation = lambda story, plan: {  # type: ignore[assignment]
        "files_written": ["tests/test_x.py"],
        "test_command_run": "pytest",
        "exit_code": 0,  # green = slop pre-implementation
        "slop_detected": True,
        "output_excerpt": "...",
        "summary": "slop: a test passed pre-implementation",
    }
    try:
        result = handle_test_implementation(s, app_config, temp_root, dry_run=True, db_path=db)
    finally:
        H._dry_run_test_implementation = original  # type: ignore[assignment]

    assert result.next_state == StoryState.BLOCKED_TESTS_NEED_CLARIFICATION
    assert s.state == StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value
    assert result.error and "slop" in result.error
