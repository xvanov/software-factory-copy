"""Tests for ``factory.chain.handlers.handle_docs_enforcer`` — clean vs violating PR files."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.app_config import AppConfig
from factory.chain.handlers import handle_docs_enforcer, persist_story
from factory.chain.state_machine import StoryRecord, StoryState


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(name="sacrifice", repo="x/y")


def _story_at_tech_writer_done(root: Path) -> StoryRecord:
    db = root / "state" / "factory.db"
    return persist_story(
        StoryRecord(
            direction_id="005",
            app="sacrifice",
            title="t",
            slug="t",
            scope="backend",
            state=StoryState.TECH_WRITER_DONE.value,
        ),
        db,
    )


def test_clean_pr_files_advance_to_pr_open(temp_root: Path, app_config: AppConfig) -> None:
    s = _story_at_tech_writer_done(temp_root)
    db = temp_root / "state" / "factory.db"
    pr_files = [
        "src/app.py",
        "tests/test_app.py",
        "context/modules/api.md",  # canonical
        "stories/42-add-healthz.md",  # canonical
    ]
    result = handle_docs_enforcer(
        s, app_config, temp_root, dry_run=True, db_path=db, pr_files=pr_files
    )
    assert result.next_state == StoryState.PR_OPEN


def test_forbidden_path_blocks_with_violation(temp_root: Path, app_config: AppConfig) -> None:
    s = _story_at_tech_writer_done(temp_root)
    db = temp_root / "state" / "factory.db"
    pr_files = [
        "src/app.py",
        "context/decisions/0001-stack.md",  # forbidden
    ]
    result = handle_docs_enforcer(
        s, app_config, temp_root, dry_run=True, db_path=db, pr_files=pr_files
    )
    assert result.next_state == StoryState.REVIEWER_REQUESTED_CHANGES
    # Payload reports the violation.
    violations = result.payload["violations"]
    assert len(violations) == 1
    assert violations[0]["path"] == "context/decisions/0001-stack.md"
    assert violations[0]["reason"] == "forbidden_path"


def test_non_canonical_path_blocks(temp_root: Path, app_config: AppConfig) -> None:
    s = _story_at_tech_writer_done(temp_root)
    db = temp_root / "state" / "factory.db"
    pr_files = ["context/random_note.md"]
    result = handle_docs_enforcer(
        s, app_config, temp_root, dry_run=True, db_path=db, pr_files=pr_files
    )
    assert result.next_state == StoryState.REVIEWER_REQUESTED_CHANGES
    assert result.payload["violations"][0]["reason"] == "not_canonical"


def test_default_pr_files_derived_from_tech_writer_json(
    temp_root: Path, app_config: AppConfig
) -> None:
    """If pr_files is None, the handler reads tech_writer_result_json paths."""
    s = _story_at_tech_writer_done(temp_root)
    db = temp_root / "state" / "factory.db"
    s.tech_writer_result_json = (
        '{"context_updates": [{"path": "context/modules/auth.md", "action": "rewrite", '
        '"content": "..."}], "rationale": ""}'
    )
    persist_story(s, db)

    result = handle_docs_enforcer(s, app_config, temp_root, dry_run=True, db_path=db)
    assert result.next_state == StoryState.PR_OPEN
    # The derived files list should include the tech-writer-claimed path.
    assert "context/modules/auth.md" in result.payload["files"]


def test_story_file_only_diff_is_vacuous(temp_root: Path, app_config: AppConfig) -> None:
    """A diff containing ONLY story files delivered nothing — the story file
    is the work order, not the work (benchmark t7, 2026-07-17: a story-file-
    only diff scanned clean because stories/*.md is canonical)."""
    s = _story_at_tech_writer_done(temp_root)
    db = temp_root / "state" / "factory.db"
    result = handle_docs_enforcer(
        s, app_config, temp_root, dry_run=True, db_path=db,
        pr_files=["stories/84-rewrite-docs.md"],
    )
    assert result.next_state == StoryState.REVIEWER_REQUESTED_CHANGES
    assert result.payload["vacuous_diff"] is True
    assert "vacuous" in (result.error or "")


def test_story_file_plus_context_change_is_not_vacuous(
    temp_root: Path, app_config: AppConfig
) -> None:
    s = _story_at_tech_writer_done(temp_root)
    db = temp_root / "state" / "factory.db"
    result = handle_docs_enforcer(
        s, app_config, temp_root, dry_run=True, db_path=db,
        pr_files=["stories/84-rewrite-docs.md", "context/modules/pipeline.md"],
    )
    assert result.next_state == StoryState.PR_OPEN
    assert "vacuous_diff" not in result.payload
