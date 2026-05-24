"""Tests for ``factory.chain.handlers.handle_tech_writer`` — dry-run + violation handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.app_config import AppConfig
from factory.chain.handlers import handle_tech_writer, persist_story
from factory.chain.state_machine import StoryRecord, StoryState


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "apps" / "sacrifice").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(name="sacrifice", repo="x/y")


def _story_at_reviewer_done(root: Path) -> StoryRecord:
    db = root / "state" / "factory.db"
    return persist_story(
        StoryRecord(
            direction_id="005",
            app="sacrifice",
            title="t",
            slug="t",
            scope="backend",
            state=StoryState.REVIEWER_DONE.value,
        ),
        db,
    )


def test_dry_run_advances_to_tech_writer_done_without_writing_files(
    temp_root: Path, app_config: AppConfig
) -> None:
    """Dry-run MUST NOT write any files to the app repo."""
    s = _story_at_reviewer_done(temp_root)
    db = temp_root / "state" / "factory.db"

    result = handle_tech_writer(s, app_config, temp_root, dry_run=True, db_path=db)
    assert result.next_state == StoryState.TECH_WRITER_DONE
    # Confirm no files written to apps/sacrifice/context/
    context_dir = temp_root / "apps" / "sacrifice" / "context"
    assert not context_dir.exists() or not list(context_dir.glob("**/*.md"))
    # tech_writer_result_json persisted.
    assert s.tech_writer_result_json is not None
    tw = json.loads(s.tech_writer_result_json)
    assert "context_updates" in tw


def test_real_run_writes_to_canonical_path(temp_root: Path, app_config: AppConfig) -> None:
    """A fixture with a canonical context update should write the file."""
    s = _story_at_reviewer_done(temp_root)
    db = temp_root / "state" / "factory.db"

    fixture = {
        "context_updates": [
            {
                "path": "context/current-state.md",
                "action": "rewrite",
                "content": "# Current state\n\nApp uses SQLite via `app/db.py`.\n",
            }
        ],
        "rationale": "Added DB module.",
    }
    result = handle_tech_writer(
        s, app_config, temp_root, dry_run=False, db_path=db, fixture=fixture
    )
    assert result.next_state == StoryState.TECH_WRITER_DONE
    target = temp_root / "apps" / "sacrifice" / "context" / "current-state.md"
    assert target.exists()
    written = target.read_text(encoding="utf-8")
    assert "SQLite" in written


def test_forbidden_path_raises_error_and_does_not_write(
    temp_root: Path, app_config: AppConfig
) -> None:
    """A fixture with a forbidden path must not write anything and must surface error."""
    s = _story_at_reviewer_done(temp_root)
    db = temp_root / "state" / "factory.db"

    fixture = {
        "context_updates": [
            {
                "path": "context/decisions/0001-foo.md",  # forbidden
                "action": "rewrite",
                "content": "blocked",
            }
        ],
        "rationale": "should be rejected",
    }
    result = handle_tech_writer(
        s, app_config, temp_root, dry_run=False, db_path=db, fixture=fixture
    )
    # State did advance to TECH_WRITER_DONE before the apply failure occurred
    # because we transitioned state at the top of the handler; the error is
    # returned in the HandlerResult so the orchestrator can flag the story.
    assert result.error and "context update failed" in result.error
    # The forbidden file was NOT written.
    forbidden = temp_root / "apps" / "sacrifice" / "context" / "decisions" / "0001-foo.md"
    assert not forbidden.exists()
