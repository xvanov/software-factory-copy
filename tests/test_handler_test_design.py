"""Tests for ``factory.chain.handlers.handle_test_design`` in dry-run mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.app_config import AppConfig
from factory.chain.handlers import handle_test_design, persist_story
from factory.chain.state_machine import StoryRecord, StoryState


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    """A throwaway software-factory root with a state dir."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "apps" / "sacrifice").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(name="sacrifice", repo="x/y", default_branch="main", context_dir="context")


def _story(root: Path) -> StoryRecord:
    db = root / "state" / "factory.db"
    s = StoryRecord(
        direction_id="002",
        app="sacrifice",
        title="Add /healthz endpoint",
        slug="add-healthz-endpoint",
        scope="backend",
        state=StoryState.STORY_CREATED.value,
    )
    return persist_story(s, db)


def test_dry_run_emits_test_plan_and_persists_json(temp_root: Path, app_config: AppConfig) -> None:
    s = _story(temp_root)
    db = temp_root / "state" / "factory.db"

    result = handle_test_design(s, app_config, temp_root, dry_run=True, db_path=db)

    # State machine advanced to TEST_DESIGN_DONE.
    assert result.next_state == StoryState.TEST_DESIGN_DONE
    # The story carries the persisted JSON plan.
    assert s.test_plan_json is not None
    plan = json.loads(s.test_plan_json)
    # Plan has at least one test with the mandatory fields.
    assert isinstance(plan["test_plan"], list) and len(plan["test_plan"]) >= 1
    test = plan["test_plan"][0]
    for required in ("name", "what_it_asserts", "tool", "file_path", "key_steps", "why_meaningful"):
        assert required in test, f"missing field {required!r} in dry-run test plan"
    # why_meaningful must not be empty — that's the slop guardrail.
    assert test["why_meaningful"].strip() != ""


def test_dry_run_e2e_required_for_frontend_scope(temp_root: Path, app_config: AppConfig) -> None:
    """A frontend-scope story should have e2e_required=True."""
    s = _story(temp_root)
    s.scope = "frontend"
    db = temp_root / "state" / "factory.db"
    persist_story(s, db)

    result = handle_test_design(s, app_config, temp_root, dry_run=True, db_path=db)
    plan = json.loads(s.test_plan_json or "{}")
    assert plan["e2e_required"] is True
    assert result.payload["plan"]["e2e_required"] is True


def test_dry_run_backend_scope_uses_pytest_tool(temp_root: Path, app_config: AppConfig) -> None:
    s = _story(temp_root)
    db = temp_root / "state" / "factory.db"
    handle_test_design(s, app_config, temp_root, dry_run=True, db_path=db)
    plan = json.loads(s.test_plan_json or "{}")
    assert plan["test_plan"][0]["tool"] == "pytest"
    assert plan["test_plan"][0]["file_path"].startswith("tests/")
