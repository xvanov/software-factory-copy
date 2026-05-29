"""Harness precheck between Test-Implementer and Dev (Item 4).

When pytest fails to *collect* (missing .env, ImportError in conftest,
missing dep), dev sees a stack trace its code cannot fix and burns the
retry budget on a config bug. The precheck runs ONCE per story (gated
by ``story.harness_precheck_passed``) inside the per-story worktree
with ONLY the test files committed. If pytest collects, dev is
dispatched normally; if it blows up with a collection failure, the
story routes to ``BLOCKED_TESTS_NEED_CLARIFICATION`` + emits a
``factory_needs_redesign`` event.

Tests cover:
  * State-machine transitions for precheck pass/fail.
  * The handler itself with fixture exit codes.
  * Orchestrator's dispatch table flips ``"dev"`` → ``"harness_precheck"``
    on first visit to TESTS_RED, and back to ``"dev"`` once the flag
    is set.
  * Real-pytest path against a real tmp git repo (collection succeeds
    on a sane worktree).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from factory.app_config import AppConfig, AppGatesConfig
from factory.chain.handlers import (
    handle_harness_precheck,
    persist_story,
)
from factory.chain.orchestrator import _dispatch_for_story
from factory.chain.state_machine import (
    EVENT_HARNESS_PRECHECK_FAIL,
    EVENT_HARNESS_PRECHECK_PASS,
    EVENT_HARNESS_PRECHECK_STARTED,
    StoryRecord,
    StoryState,
    advance,
)
from factory.chain.event_log import read_story_events


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_state_machine_tests_red_to_harness_precheck() -> None:
    story = StoryRecord(
        direction_id="d",
        app="x",
        title="t",
        slug="s",
        scope="backend",
        state=StoryState.TESTS_RED.value,
    )
    nxt = advance(story, EVENT_HARNESS_PRECHECK_STARTED)
    assert nxt == StoryState.HARNESS_PRECHECK_IN_PROGRESS


def test_state_machine_pass_returns_to_tests_red() -> None:
    """Pass loops back to TESTS_RED — the flag on the story tells the
    orchestrator to skip the precheck the next time around."""
    story = StoryRecord(
        direction_id="d",
        app="x",
        title="t",
        slug="s",
        scope="backend",
        state=StoryState.HARNESS_PRECHECK_IN_PROGRESS.value,
    )
    nxt = advance(story, EVENT_HARNESS_PRECHECK_PASS)
    assert nxt == StoryState.TESTS_RED


def test_state_machine_fail_routes_to_blocked() -> None:
    story = StoryRecord(
        direction_id="d",
        app="x",
        title="t",
        slug="s",
        scope="backend",
        state=StoryState.HARNESS_PRECHECK_IN_PROGRESS.value,
    )
    nxt = advance(story, EVENT_HARNESS_PRECHECK_FAIL)
    assert nxt == StoryState.BLOCKED_TESTS_NEED_CLARIFICATION


# ---------------------------------------------------------------------------
# Orchestrator dispatch
# ---------------------------------------------------------------------------


def test_dispatch_tests_red_first_visit_picks_harness_precheck() -> None:
    story = StoryRecord(
        direction_id="d",
        app="x",
        title="t",
        slug="s",
        scope="backend",
        state=StoryState.TESTS_RED.value,
        harness_precheck_passed=False,
    )
    assert _dispatch_for_story(story) == "harness_precheck"


def test_dispatch_tests_red_after_pass_picks_dev() -> None:
    story = StoryRecord(
        direction_id="d",
        app="x",
        title="t",
        slug="s",
        scope="backend",
        state=StoryState.TESTS_RED.value,
        harness_precheck_passed=True,
    )
    assert _dispatch_for_story(story) == "dev"


def test_dispatch_dev_retry_always_picks_dev() -> None:
    """DEV_RETRY bypasses the precheck regardless of the flag — precheck
    is once-per-story, not once-per-dev-attempt."""
    story = StoryRecord(
        direction_id="d",
        app="x",
        title="t",
        slug="s",
        scope="backend",
        state=StoryState.DEV_RETRY.value,
        harness_precheck_passed=False,
    )
    assert _dispatch_for_story(story) == "dev"


# ---------------------------------------------------------------------------
# handle_harness_precheck — fixture-driven exit codes
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "apps" / "myapp" / "stories").mkdir(parents=True, exist_ok=True)
    (tmp_path / "apps" / "myapp" / "stories" / "1-x.md").write_text(
        "# story\n", encoding="utf-8"
    )
    src = tmp_path / "myapp"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=str(src), check=True)
    subprocess.run(["git", "config", "user.email", "t@e.x"], cwd=str(src), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(src), check=True)
    (src / "README.md").write_text("# init\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(src), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(src), check=True)
    return tmp_path


def _story(temp_root: Path) -> StoryRecord:
    return persist_story(
        StoryRecord(
            id=None,
            direction_id="099",
            app="myapp",
            title="t",
            slug="z",
            scope="backend",
            state=StoryState.TESTS_RED.value,
            github_issue_number=1,
            story_file_path="stories/1-x.md",
        ),
        temp_root / "state" / "factory.db",
    )


def _cfg(temp_root: Path) -> AppConfig:
    return AppConfig(
        name="myapp",
        repo="x/y",
        default_branch="main",
        app_repo_path=str(temp_root / "myapp"),
        gates=AppGatesConfig(test_command="echo ignored"),
    )


def test_precheck_pass_sets_flag_and_returns_to_tests_red(
    temp_root: Path,
) -> None:
    story = _story(temp_root)
    db = temp_root / "state" / "factory.db"

    result = handle_harness_precheck(
        story,
        _cfg(temp_root),
        temp_root,
        dry_run=True,
        db_path=db,
        fixture_exit_code=1,
        fixture_output="2 failed, 0 errors",
    )

    assert result.next_state == StoryState.TESTS_RED
    assert story.harness_precheck_passed is True
    assert result.payload["passed"] is True
    assert result.error is None


def test_precheck_fail_blocks_and_emits_redesign_event(temp_root: Path) -> None:
    """Collection failure (exit 2) blocks the story and writes a
    ``factory_needs_redesign`` event to the per-story log."""
    story = _story(temp_root)
    db = temp_root / "state" / "factory.db"

    result = handle_harness_precheck(
        story,
        _cfg(temp_root),
        temp_root,
        dry_run=True,
        db_path=db,
        fixture_exit_code=2,
        fixture_output="ERROR collecting test session: ImportError",
    )

    assert result.next_state == StoryState.BLOCKED_TESTS_NEED_CLARIFICATION
    assert story.harness_precheck_passed is False
    assert result.payload["passed"] is False
    assert result.error is not None and "harness_precheck_failed" in result.error
    events = read_story_events(story.id, software_factory_root=temp_root, slug_hint=story.slug)
    kinds = [e["event"] for e in events]
    assert "factory_needs_redesign" in kinds
    redesign = next(e for e in events if e["event"] == "factory_needs_redesign")
    assert redesign.get("kind") == "harness_failure"


@pytest.mark.parametrize("bad_code", [3, 4, 5, 124, 99])
def test_precheck_recognises_all_collection_failure_codes(
    temp_root: Path, bad_code: int
) -> None:
    """pytest exit 2/3/4/5 + harness timeout (124) + raise (99) all
    route to BLOCKED. Only 0 and 1 are accepted as 'harness OK'."""
    story = _story(temp_root)
    result = handle_harness_precheck(
        story,
        _cfg(temp_root),
        temp_root,
        dry_run=True,
        db_path=temp_root / "state" / "factory.db",
        fixture_exit_code=bad_code,
    )
    assert result.next_state == StoryState.BLOCKED_TESTS_NEED_CLARIFICATION


@pytest.mark.parametrize("ok_code", [0, 1])
def test_precheck_accepts_zero_and_one(temp_root: Path, ok_code: int) -> None:
    """0 (all pass — unusual pre-dev but harness OK) and 1 (collected
    + tests failed — the desired pre-dev state) both pass precheck."""
    story = _story(temp_root)
    result = handle_harness_precheck(
        story,
        _cfg(temp_root),
        temp_root,
        dry_run=True,
        db_path=temp_root / "state" / "factory.db",
        fixture_exit_code=ok_code,
    )
    assert result.next_state == StoryState.TESTS_RED
    assert story.harness_precheck_passed is True


# ---------------------------------------------------------------------------
# Real-pytest path
# ---------------------------------------------------------------------------


def test_precheck_real_pytest_against_clean_worktree(tmp_path: Path) -> None:
    """End-to-end: a worktree with a tiny failing pytest file collects
    cleanly and the precheck passes."""
    root = tmp_path / "factory"
    root.mkdir()
    (root / "state").mkdir()
    (root / "apps" / "myapp" / "stories").mkdir(parents=True)
    (root / "apps" / "myapp" / "stories" / "1-x.md").write_text(
        "# story\n", encoding="utf-8"
    )

    src = tmp_path / "myapp"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=str(src), check=True)
    subprocess.run(["git", "config", "user.email", "t@e.x"], cwd=str(src), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(src), check=True)
    (src / "tests").mkdir()
    (src / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (src / "tests" / "test_x.py").write_text(
        "def test_fails():\n    assert 1 == 2\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=str(src), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(src), check=True)

    cfg = AppConfig(
        name="myapp",
        repo="x/y",
        default_branch="main",
        app_repo_path=str(src),
        gates=AppGatesConfig(test_command="python -m pytest -q tests/"),
    )
    story = persist_story(
        StoryRecord(
            id=None,
            direction_id="099",
            app="myapp",
            title="t",
            slug="real",
            scope="backend",
            state=StoryState.TESTS_RED.value,
            github_issue_number=1,
            story_file_path="stories/1-x.md",
        ),
        root / "state" / "factory.db",
    )

    result = handle_harness_precheck(
        story,
        cfg,
        root,
        dry_run=False,
        db_path=root / "state" / "factory.db",
    )

    # The precheck now runs collection-only (--collect-only): real pytest
    # collects the suite successfully => exit 0 => precheck passes. (It no
    # longer executes the tests, so we don't see the exit-1 "red" code — the
    # point of the precheck is collectability, not red/green.)
    assert result.next_state == StoryState.TESTS_RED
    assert story.harness_precheck_passed is True
    assert result.payload["exit_code"] == 0


def test_precheck_real_pytest_against_broken_conftest_fails(
    tmp_path: Path,
) -> None:
    """An ImportError in conftest is exactly the failure mode the
    precheck is designed to catch — pytest exits with code 2 (or 3)
    and the precheck blocks the story."""
    root = tmp_path / "factory"
    root.mkdir()
    (root / "state").mkdir()
    (root / "apps" / "myapp" / "stories").mkdir(parents=True)
    (root / "apps" / "myapp" / "stories" / "1-x.md").write_text(
        "# story\n", encoding="utf-8"
    )

    src = tmp_path / "myapp"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=str(src), check=True)
    subprocess.run(["git", "config", "user.email", "t@e.x"], cwd=str(src), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(src), check=True)
    (src / "tests").mkdir()
    (src / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (src / "tests" / "conftest.py").write_text(
        "import this_module_does_not_exist_anywhere\n", encoding="utf-8"
    )
    (src / "tests" / "test_x.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=str(src), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(src), check=True)

    cfg = AppConfig(
        name="myapp",
        repo="x/y",
        default_branch="main",
        app_repo_path=str(src),
        gates=AppGatesConfig(test_command="python -m pytest -q tests/"),
    )
    story = persist_story(
        StoryRecord(
            id=None,
            direction_id="099",
            app="myapp",
            title="t",
            slug="broken",
            scope="backend",
            state=StoryState.TESTS_RED.value,
            github_issue_number=1,
            story_file_path="stories/1-x.md",
        ),
        root / "state" / "factory.db",
    )

    result = handle_harness_precheck(
        story,
        cfg,
        root,
        dry_run=False,
        db_path=root / "state" / "factory.db",
    )

    assert result.next_state == StoryState.BLOCKED_TESTS_NEED_CLARIFICATION
    assert story.harness_precheck_passed is False
    # pytest emits 2 (interrupted/usage), 3 (internal), or 4 (command-line
    # usage error / collection-time ImportError depending on version).
    assert result.payload["exit_code"] in {2, 3, 4}
