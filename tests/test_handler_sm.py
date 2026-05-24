"""Tests for ``factory.chain.handlers.handle_sm`` (dry-run + fixture path)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.app_config import AppConfig
from factory.chain.handlers import handle_sm, persist_story
from factory.chain.state_machine import StoryRecord, StoryState


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    """A throwaway software-factory root with state and apps dirs."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "apps" / "sacrifice").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(name="sacrifice", repo="x/y", default_branch="main", context_dir="context")


def _write_direction(
    root: Path,
    *,
    dir_id: str = "002",
    slug: str = "add-healthz-endpoint",
    flow: str | None = None,
    api: str | None = None,
    acceptance: list[str] | None = None,
) -> None:
    """Write a minimal direction dir on disk so ``find_direction_for_story``
    resolves it."""
    direction_dir = root / "apps" / "sacrifice" / "directions" / f"{dir_id}-{slug}"
    direction_dir.mkdir(parents=True, exist_ok=True)
    ac_block = ""
    if acceptance:
        ac_block = "\n## Acceptance Criteria\n\n" + "\n".join(f"- {a}" for a in acceptance) + "\n"
    (direction_dir / "direction.md").write_text(
        f"---\ntitle: {slug}\n---\n\n# {slug}\n\n## Why\n\nbecause.\n{ac_block}",
        encoding="utf-8",
    )
    if flow is not None:
        (direction_dir / "flow.md").write_text(flow, encoding="utf-8")
    if api is not None:
        (direction_dir / "api_spec.md").write_text(api, encoding="utf-8")


def _story(root: Path, *, scope: str = "backend") -> StoryRecord:
    db = root / "state" / "factory.db"
    s = StoryRecord(
        direction_id="002",
        app="sacrifice",
        title="Add /healthz endpoint",
        slug="add-healthz-endpoint",
        scope=scope,
        state=StoryState.STORY_CREATED.value,
        story_file_path="stories/0-add-healthz-endpoint.md",
    )
    return persist_story(s, db)


def test_dry_run_advances_through_sm_and_writes_story_file(
    temp_root: Path, app_config: AppConfig
) -> None:
    """Dry-run: SM transitions STORY_CREATED -> SM_IN_PROGRESS -> SM_DONE and
    writes a BMAD-format story file at the slug-based path."""
    _write_direction(temp_root, acceptance=["/healthz returns {version, status}"])
    s = _story(temp_root)
    db = temp_root / "state" / "factory.db"

    result = handle_sm(s, app_config, temp_root, dry_run=True, db_path=db)

    assert result.next_state == StoryState.SM_DONE
    assert s.sm_result_json is not None
    target = temp_root / "apps" / "sacrifice" / "stories" / "0-add-healthz-endpoint.md"
    assert target.exists(), f"expected story at {target}"
    text = target.read_text(encoding="utf-8")
    assert "Acceptance Criteria" in text
    assert "/healthz returns {version, status}" in text


def test_dry_run_embeds_flow_verbatim_when_present(temp_root: Path, app_config: AppConfig) -> None:
    """If the direction provides flow.md, the SM story file MUST embed it verbatim."""
    user_flow = (
        "# Flow\n\n"
        "1. User taps `Pledge`.\n"
        "2. User enters $5 and submits.\n"
        "3. User sees `Pledged $5` toast.\n"
    )
    _write_direction(temp_root, flow=user_flow)
    s = _story(temp_root, scope="frontend")
    db = temp_root / "state" / "factory.db"
    handle_sm(s, app_config, temp_root, dry_run=True, db_path=db)

    target = temp_root / "apps" / "sacrifice" / "stories" / "0-add-healthz-endpoint.md"
    text = target.read_text(encoding="utf-8")
    # Verbatim embed — must contain every meaningful line of flow.md.
    for line in ("User taps `Pledge`.", "enters $5 and submits.", "Pledged $5"):
        assert line in text, f"expected {line!r} verbatim in story file"


def test_dry_run_embeds_api_spec_verbatim_when_present(
    temp_root: Path, app_config: AppConfig
) -> None:
    api_spec = "## /healthz\n\n`GET /healthz` -> 200 `{version, status}`\n"
    _write_direction(temp_root, api=api_spec)
    s = _story(temp_root)
    db = temp_root / "state" / "factory.db"
    handle_sm(s, app_config, temp_root, dry_run=True, db_path=db)
    target = temp_root / "apps" / "sacrifice" / "stories" / "0-add-healthz-endpoint.md"
    assert "GET /healthz" in target.read_text(encoding="utf-8")


def test_story_file_path_is_persisted(temp_root: Path, app_config: AppConfig) -> None:
    """After SM, story.story_file_path is the slug-based relative path."""
    _write_direction(temp_root)
    s = _story(temp_root)
    db = temp_root / "state" / "factory.db"
    handle_sm(s, app_config, temp_root, dry_run=True, db_path=db)
    assert s.story_file_path == "stories/0-add-healthz-endpoint.md"


def test_fixture_overrides_dry_run(temp_root: Path, app_config: AppConfig) -> None:
    """A test-supplied fixture is used as-is and persisted to sm_result_json."""
    _write_direction(temp_root)
    s = _story(temp_root)
    db = temp_root / "state" / "factory.db"
    fixture = {
        "stories": [
            {
                "title": "Add /healthz endpoint",
                "slug": "add-healthz-endpoint",
                "scope": "backend",
                "file_content": "# Custom story content from fixture\n",
                "target_path": "stories/0-add-healthz-endpoint.md",
            }
        ],
        "summary": "fixture story",
    }
    handle_sm(s, app_config, temp_root, dry_run=True, db_path=db, fixture=fixture)
    parsed = json.loads(s.sm_result_json or "{}")
    assert parsed["summary"] == "fixture story"
    target = temp_root / "apps" / "sacrifice" / "stories" / "0-add-healthz-endpoint.md"
    assert target.read_text(encoding="utf-8") == "# Custom story content from fixture\n"
