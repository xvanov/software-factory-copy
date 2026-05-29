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

import subprocess
from pathlib import Path

import pytest

from factory.app_config import AppConfig, AppGatesConfig
from factory.chain.event_log import read_story_events
from factory.chain.handlers import (
    _collection_failure_is_module_under_construction,
    _first_party_package_exists,
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


# ---------------------------------------------------------------------------
# Module-under-construction reclassification
# ---------------------------------------------------------------------------
#
# A brand-new-module story writes a test that imports the module it is meant to
# create. At --collect-only time that import raises ModuleNotFoundError and
# pytest exits 2. That is a legitimate TDD red — NOT environmental breakage —
# so the precheck must let dev proceed (dev's job is to create the module).
# A missing third-party dependency or a broken conftest is still real
# breakage and must keep blocking.


def _make_worktree_with_app_package(tmp_path: Path) -> Path:
    """Worktree laid out like the real backend: a first-party ``app`` package
    one level down (``backend/app/``), but no ``app/models/media_upload.py``."""
    wt = tmp_path / "wt"
    (wt / "backend" / "app" / "models").mkdir(parents=True)
    (wt / "backend" / "app" / "__init__.py").write_text("", encoding="utf-8")
    (wt / "backend" / "app" / "models" / "__init__.py").write_text("", encoding="utf-8")
    return wt


_MUC_OUTPUT = (
    "tests/test_media_uploads_model.py:11: in <module>\n"
    "    from app.models.media_upload import MediaUpload\n"
    "E   ModuleNotFoundError: No module named 'app.models.media_upload'\n"
    "ERROR tests/test_media_uploads_model.py\n"
    "!!! Interrupted: 1 error during collection !!!\n"
)


def test_first_party_package_exists_detects_nested_package(tmp_path: Path) -> None:
    wt = _make_worktree_with_app_package(tmp_path)
    assert _first_party_package_exists(wt, "app") is True
    assert _first_party_package_exists(wt, "redis") is False


def test_classifier_true_for_first_party_module(tmp_path: Path) -> None:
    wt = _make_worktree_with_app_package(tmp_path)
    assert _collection_failure_is_module_under_construction(_MUC_OUTPUT, wt) is True


def test_classifier_false_for_missing_third_party_dep(tmp_path: Path) -> None:
    wt = _make_worktree_with_app_package(tmp_path)
    out = (
        "tests/test_x.py:3: in <module>\n    import redis\n"
        "E   ModuleNotFoundError: No module named 'redis'\n"
    )
    assert _collection_failure_is_module_under_construction(out, wt) is False


def test_classifier_false_for_conftest_failure(tmp_path: Path) -> None:
    wt = _make_worktree_with_app_package(tmp_path)
    out = (
        "tests/conftest.py:2: in <module>\n    from app.models.media_upload import X\n"
        "E   ModuleNotFoundError: No module named 'app.models.media_upload'\n"
    )
    # conftest breakage is shared-infra → never under-construction.
    assert _collection_failure_is_module_under_construction(out, wt) is False


def test_classifier_false_when_any_import_is_third_party(tmp_path: Path) -> None:
    wt = _make_worktree_with_app_package(tmp_path)
    out = _MUC_OUTPUT + "E   ModuleNotFoundError: No module named 'boto3'\n"
    # One genuinely-missing dep is enough to keep the story blocked.
    assert _collection_failure_is_module_under_construction(out, wt) is False


def test_classifier_false_when_no_import_error(tmp_path: Path) -> None:
    wt = _make_worktree_with_app_package(tmp_path)
    out = "E   SyntaxError: invalid syntax\n"
    assert _collection_failure_is_module_under_construction(out, wt) is False


def test_precheck_reclassifies_module_under_construction_as_pass(
    temp_root: Path, tmp_path: Path
) -> None:
    """exit 2 + first-party module import error + a worktree where that
    package exists → precheck PASSES (dev gets dispatched to build it)."""
    story = _story(temp_root)
    wt = _make_worktree_with_app_package(tmp_path)
    result = handle_harness_precheck(
        story,
        _cfg(temp_root),
        temp_root,
        dry_run=False,
        db_path=temp_root / "state" / "factory.db",
        fixture_exit_code=2,
        fixture_output=_MUC_OUTPUT,
        fixture_worktree_root=wt,
    )
    assert result.next_state == StoryState.TESTS_RED
    assert story.harness_precheck_passed is True
    assert result.payload["passed"] is True
    assert result.payload["module_under_construction"] is True
    assert result.error is None


def test_precheck_missing_dep_still_blocks_even_with_worktree(
    temp_root: Path, tmp_path: Path
) -> None:
    """exit 2 caused by a missing third-party dep is NOT reclassified —
    the story still blocks for operator attention."""
    story = _story(temp_root)
    wt = _make_worktree_with_app_package(tmp_path)
    result = handle_harness_precheck(
        story,
        _cfg(temp_root),
        temp_root,
        dry_run=False,
        db_path=temp_root / "state" / "factory.db",
        fixture_exit_code=2,
        fixture_output="E   ModuleNotFoundError: No module named 'redis'\n",
        fixture_worktree_root=wt,
    )
    assert result.next_state == StoryState.BLOCKED_TESTS_NEED_CLARIFICATION
    assert story.harness_precheck_passed is False


def test_precheck_real_pytest_new_module_import_reclassified(tmp_path: Path) -> None:
    """End-to-end reproduction of the observed bug: a worktree with a real
    first-party ``app`` package and a test importing a not-yet-created
    submodule. Real pytest --collect-only exits 2; the precheck reclassifies
    it as module-under-construction and passes."""
    root = tmp_path / "factory"
    (root / "state").mkdir(parents=True)
    (root / "apps" / "myapp" / "stories").mkdir(parents=True)
    (root / "apps" / "myapp" / "stories" / "1-x.md").write_text("# story\n", encoding="utf-8")

    src = tmp_path / "myapp"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=str(src), check=True)
    subprocess.run(["git", "config", "user.email", "t@e.x"], cwd=str(src), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(src), check=True)
    # First-party package exists; the target submodule does NOT.
    (src / "app" / "models").mkdir(parents=True)
    (src / "app" / "__init__.py").write_text("", encoding="utf-8")
    (src / "app" / "models" / "__init__.py").write_text("", encoding="utf-8")
    (src / "tests").mkdir()
    (src / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (src / "tests" / "test_new_model.py").write_text(
        "from app.models.media_upload import MediaUpload\n\n"
        "def test_exists():\n    assert MediaUpload\n",
        encoding="utf-8",
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
            slug="newmod",
            scope="backend",
            state=StoryState.TESTS_RED.value,
            github_issue_number=1,
            story_file_path="stories/1-x.md",
        ),
        root / "state" / "factory.db",
    )

    result = handle_harness_precheck(
        story, cfg, root, dry_run=False, db_path=root / "state" / "factory.db"
    )

    assert result.payload["exit_code"] == 2
    assert result.payload["module_under_construction"] is True
    assert result.next_state == StoryState.TESTS_RED
    assert story.harness_precheck_passed is True
