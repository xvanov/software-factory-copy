"""Unit tests for the Phase-3 test-plan validator (_validate_test_plan)."""
from __future__ import annotations

from factory.app_config import AppGatesConfig
from factory.chain.handlers import _validate_test_plan
from factory.chain.state_machine import StoryRecord, StoryState


def _story(scope: str) -> StoryRecord:
    return StoryRecord(id=1, direction_id="1", app="x", title="t", slug="s",
                       scope=scope, state=StoryState.TEST_DESIGN_DONE.value)


def test_playwright_flagged_when_no_harness() -> None:
    plan = {"e2e_required": True, "test_plan": [
        {"name": "flow", "tool": "playwright", "file_path": "e2e/flow.spec.ts"}]}
    v = _validate_test_plan(plan, _story("frontend"), AppGatesConfig(e2e_harness_ready=False))
    assert any("playwright" in s for s in v)
    assert any("e2e_required" in s for s in v)


def test_clean_when_harness_ready() -> None:
    plan = {"e2e_required": True, "test_plan": [
        {"name": "flow", "tool": "playwright", "file_path": "e2e/flow.spec.ts"}]}
    assert _validate_test_plan(plan, _story("frontend"), AppGatesConfig(e2e_harness_ready=True)) == []


def test_backend_test_in_frontend_story_flagged() -> None:
    plan = {"e2e_required": False, "test_plan": [
        {"name": "api", "tool": "pytest", "file_path": "backend/tests/test_uploads.py"}]}
    v = _validate_test_plan(plan, _story("frontend"), AppGatesConfig(e2e_harness_ready=False))
    assert any("out of scope" in s for s in v)


def test_backend_pytest_in_backend_story_is_clean() -> None:
    plan = {"e2e_required": False, "test_plan": [
        {"name": "api", "tool": "pytest", "file_path": "backend/tests/test_uploads.py"}]}
    assert _validate_test_plan(plan, _story("backend"), AppGatesConfig(e2e_harness_ready=False)) == []
