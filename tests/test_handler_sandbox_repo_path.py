"""Regression test for Bug A: sandbox runs MUST use the real app source tree.

The chain previously called ``sandbox_run(..., repo_path=software_factory_root /
'apps' / story.app)`` — pointing the OpenHands SDK at the factory's per-app
*metadata* directory (config.yaml, directions/, stories/) instead of the
real app source. Side effects:

* The SDK's ``git commit`` calls landed on factory main rather than the
  target repo's feature branch (one production commit ``3f2082a`` got
  reset out before this fix landed).
* The chain's pytest gate ran in the metadata directory, which has no
  tests — every dev run got a spurious "tests not green" verdict.

This module exists to make sure both ``handle_test_implementation`` and
``handle_dev`` keep passing the ``resolve_app_repo_path(...)`` result.
We mock ``sandbox_run`` and assert on the ``repo_path`` keyword.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from factory import runner as runner_module
from factory.app_config import AppConfig
from factory.chain import handlers as handlers_module
from factory.chain.handlers import handle_dev, handle_test_implementation, persist_story
from factory.chain.state_machine import StoryRecord, StoryState
from factory.runner import RunResult


def _init_repo(path: Path, *, default_branch: str = "main") -> None:
    """Fresh git repo with one initial commit on ``default_branch``."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", f"--initial-branch={default_branch}"],
        cwd=str(path),
        check=True,
    )
    subprocess.run(["git", "config", "user.email", "t@e.x"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "T E"], cwd=str(path), check=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(path), check=True)


@pytest.fixture
def fixture_tree(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """Build (factory_root, target_app_repo) on disk.

    The factory side has the per-app metadata layout the handlers expect to
    READ from (story file etc.). The target app repo is a real git repo —
    the place every sandbox commit MUST go.
    """
    factory_root = tmp_path / "software-factory"
    (factory_root / "state").mkdir(parents=True)
    (factory_root / "apps" / "sacrifice" / "stories").mkdir(parents=True)
    (factory_root / "apps" / "sacrifice" / "stories" / "1-x.md").write_text(
        "# story\n", encoding="utf-8"
    )

    target = tmp_path / "sacrifice"
    _init_repo(target)
    yield factory_root, target


def _story_at(state: StoryState, factory_root: Path) -> StoryRecord:
    return persist_story(
        StoryRecord(
            id=None,
            direction_id="005",
            app="sacrifice",
            title="t",
            slug="x",
            scope="backend",
            state=state.value,
            github_issue_number=1,
            story_file_path="stories/1-x.md",
        ),
        factory_root / "state" / "factory.db",
    )


def _app_config_pointing_at(target: Path) -> AppConfig:
    return AppConfig(
        name="sacrifice",
        repo="x/y",
        default_branch="main",
        app_repo_path=str(target),
    )


def test_handle_test_implementation_uses_real_app_repo(
    fixture_tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``handle_test_implementation`` must pass the resolved app repo path —
    not ``software_factory_root/apps/<app>/`` — to ``sandbox_run``.

    Captures the ``repo_path`` kwarg the handler hands to sandbox_run and
    asserts it equals the resolved target repo. The factory's per-app metadata
    dir under ``apps/<app>/`` MUST NOT appear here.
    """
    factory_root, target = fixture_tree
    app_cfg = _app_config_pointing_at(target)
    story = _story_at(StoryState.TEST_DESIGN_DONE, factory_root)

    captured: dict[str, Any] = {}

    async def _fake_sandbox_run(*args: Any, **kwargs: Any) -> RunResult:
        captured["repo_path"] = kwargs.get("repo_path")
        captured["persona"] = kwargs.get("persona")
        captured["story_path"] = kwargs.get("story_path")
        # Return a benign "tests are red" result so test_implementation_done
        # transitions to TESTS_RED (the desired-outcome branch).
        return RunResult(
            success=False,
            files_changed=[],
            test_run_passed=False,
            tokens_in=1,
            tokens_out=1,
            cost_usd=0.0,
            summary="fake test_impl",
        )

    monkeypatch.setattr(runner_module, "sandbox_run", _fake_sandbox_run, raising=True)
    monkeypatch.setattr(handlers_module, "route", lambda *a, **kw: "azure/deepseek-v4-pro")

    handle_test_implementation(
        story,
        app_cfg,
        factory_root,
        dry_run=False,
        db_path=factory_root / "state" / "factory.db",
    )

    assert captured["persona"] == "test_implementer"
    assert captured["repo_path"] == target, (
        f"repo_path should be the real app source tree {target}, "
        f"got {captured['repo_path']!r}. This is Bug A: the sandbox was "
        f"pointed at the factory's metadata dir."
    )
    # The story file lives under the factory tree, not the app repo.
    assert str(captured["story_path"]).startswith(str(factory_root)), (
        "story_path should live under the factory metadata tree"
    )


def test_handle_dev_uses_real_app_repo(
    fixture_tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same as the test above but for ``handle_dev``. Dev's commits would
    otherwise land on factory main if the sandbox were pointed at the wrong
    tree — the bug that produced commit ``3f2082a`` before this fix.
    """
    factory_root, target = fixture_tree
    app_cfg = _app_config_pointing_at(target)
    story = _story_at(StoryState.TESTS_RED, factory_root)

    captured: dict[str, Any] = {}

    async def _fake_sandbox_run(*args: Any, **kwargs: Any) -> RunResult:
        captured["repo_path"] = kwargs.get("repo_path")
        captured["persona"] = kwargs.get("persona")
        return RunResult(
            success=True,
            files_changed=["src/x.py"],
            test_run_passed=True,
            tokens_in=1,
            tokens_out=1,
            cost_usd=0.0,
            summary="fake dev",
        )

    monkeypatch.setattr(runner_module, "sandbox_run", _fake_sandbox_run, raising=True)
    monkeypatch.setattr(handlers_module, "route", lambda *a, **kw: "azure/deepseek-v4-pro")

    handle_dev(
        story,
        app_cfg,
        factory_root,
        dry_run=False,
        db_path=factory_root / "state" / "factory.db",
    )

    assert captured["persona"] == "dev"
    assert captured["repo_path"] == target, (
        f"repo_path should be the real app source tree {target}, "
        f"got {captured['repo_path']!r}. Bug A regression."
    )
