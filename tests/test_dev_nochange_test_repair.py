"""Dev-made-no-progress → route to test repair (loop-3 systemic fix).

When a dev run produces ZERO file changes yet tests stay red, re-running dev is
futile: unchanged code reproduces the identical failure. The dominant cause is
a test-quality defect only the test_implementer can fix (contradictory /
impossible / contract-mismatched tests). This used to march stories straight
into a terminal block (no story should ever block on a factory-fixable cause).

The handler now detects the no-change signal and routes back to the test loop
(via EVENT_TESTS_NEED_CLARIFICATION → TEST_DESIGN_DONE) with dev's diagnosis as
the repair brief, WITHOUT consuming the dev retry budget, capped so a genuinely
unsatisfiable contract eventually surfaces a SPECIFIC block signal.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from factory import runner as runner_module
from factory.app_config import AppConfig
from factory.chain import handlers as handlers_module
from factory.chain.event_log import read_story_events
from factory.chain.handlers import (
    _MAX_DEV_NOCHANGE_TEST_REPAIRS,
    handle_dev,
    persist_story,
)
from factory.chain.state_machine import StoryRecord, StoryState
from factory.runner import RunResult


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "apps" / "myapp" / "stories").mkdir(parents=True, exist_ok=True)
    (tmp_path / "apps" / "myapp" / "stories" / "1-x.md").write_text("# story\n", encoding="utf-8")
    src = tmp_path / "myapp"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=str(src), check=True)
    subprocess.run(["git", "config", "user.email", "t@e.x"], cwd=str(src), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(src), check=True)
    (src / "README.md").write_text("# init\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(src), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(src), check=True)
    return tmp_path


@pytest.fixture
def app_config(temp_root: Path) -> AppConfig:
    return AppConfig(
        name="myapp", repo="x/y", default_branch="main",
        app_repo_path=str(temp_root / "myapp"),
    )


def _story(root: Path) -> StoryRecord:
    return persist_story(
        StoryRecord(
            id=None, direction_id="099", app="myapp", title="t", slug="z",
            scope="backend", state=StoryState.TESTS_RED.value,
            github_issue_number=1, story_file_path="stories/1-x.md",
        ),
        root / "state" / "factory.db",
    )


def _nochange_sandbox(diagnosis: str = "The 404 and 501 tests assert different results for the SAME nonexistent id — mutually exclusive."):
    async def _fake(*args: object, **kwargs: object) -> RunResult:
        return RunResult(
            success=True,                # the run itself worked...
            files_changed=[],            # ...but dev changed nothing
            test_run_passed=False,       # ...and tests stayed red
            tokens_in=1500, tokens_out=600, cost_usd=0.05,
            error="tests not green after run",
            summary="1 failed, 90 passed",
            self_summary=diagnosis,
        )
    return _fake


def test_nochange_routes_to_test_repair_without_burning_budget(
    temp_root: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    story = _story(temp_root)
    db = temp_root / "state" / "factory.db"
    monkeypatch.setattr(runner_module, "sandbox_run", _nochange_sandbox(), raising=True)
    monkeypatch.setattr(handlers_module, "route", lambda *a, **kw: "azure/gpt-5.4")

    result = handle_dev(story, app_config, temp_root, dry_run=False, db_path=db)

    # Routed back to the test loop, not blocked.
    assert result.next_state is StoryState.TEST_DESIGN_DONE
    assert story.dev_retries == 0  # budget preserved
    # Dev's diagnosis was handed to the test loop as a repair brief.
    rrj = json.loads(story.reviewer_result_json)
    assert rrj["test_quality_findings"]
    assert "mutually exclusive" in rrj["test_quality_findings"][0]["issue"]
    events = read_story_events(story.id, software_factory_root=temp_root, slug_hint=story.slug)
    repair = [e for e in events if e.get("event") == "dev_nochange_test_repair"]
    assert len(repair) == 1 and repair[0]["attempt"] == 1
    assert not [e for e in events if e.get("event") == "dev_retry"]


def test_nochange_blocks_specifically_after_cap(
    temp_root: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    story = _story(temp_root)
    db = temp_root / "state" / "factory.db"
    monkeypatch.setattr(runner_module, "sandbox_run", _nochange_sandbox(), raising=True)
    monkeypatch.setattr(handlers_module, "route", lambda *a, **kw: "azure/gpt-5.4")

    for _ in range(_MAX_DEV_NOCHANGE_TEST_REPAIRS):
        r = handle_dev(story, app_config, temp_root, dry_run=False, db_path=db)
        assert r.next_state is StoryState.TEST_DESIGN_DONE
        story.state = StoryState.TESTS_RED.value  # chain returns it to dev after repair
        persist_story(story, db)

    final = handle_dev(story, app_config, temp_root, dry_run=False, db_path=db)
    assert final.next_state is StoryState.BLOCKED_TESTS_NEED_CLARIFICATION
    assert story.dev_retries == 0
    assert "unsatisfiable" in (story.error or "")
    events = read_story_events(story.id, software_factory_root=temp_root, slug_hint=story.slug)
    redesign = [
        e for e in events
        if e.get("event") == "factory_needs_redesign"
        and e.get("kind") == "tests_unsatisfiable_no_dev_progress"
    ]
    assert len(redesign) == 1


def test_dev_with_real_changes_still_uses_normal_retry(
    temp_root: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A red run where dev DID change code is a normal retry, not test-repair."""
    story = _story(temp_root)
    db = temp_root / "state" / "factory.db"

    async def _fake(*args: object, **kwargs: object) -> RunResult:
        return RunResult(
            success=True, files_changed=["src/x.py"], test_run_passed=False,
            tokens_in=1500, tokens_out=600, cost_usd=0.05,
            error="tests not green after run", summary="AssertionError",
        )

    monkeypatch.setattr(runner_module, "sandbox_run", _fake, raising=True)
    monkeypatch.setattr(handlers_module, "route", lambda *a, **kw: "azure/gpt-5.4")

    result = handle_dev(story, app_config, temp_root, dry_run=False, db_path=db)
    assert result.next_state is StoryState.DEV_RETRY
    assert story.dev_retries == 1
    events = read_story_events(story.id, software_factory_root=temp_root, slug_hint=story.slug)
    assert not [e for e in events if e.get("event") == "dev_nochange_test_repair"]
