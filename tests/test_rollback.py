"""Tests for the rollback worker.

Dry-run with fixture CI states. Verifies:

  * red main CI → revert + p0 issue + mode flip
  * green main CI → no_op (no mode change)
  * merges outside the window are ignored
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, create_engine, select

from factory.chain.auto_merge import FixturePR, MergeActionRecord, auto_merge_tick
from factory.chain.rollback import RollbackActionRecord, rollback_watch_tick
from factory.chain.state_machine import StoryRecord, StoryState
from factory.settings.modes import get_mode


@pytest.fixture
def factory_root_with_recent_merge(tmp_path: Path) -> Path:
    """Set up a factory root + record one recent successful merge."""
    apps = tmp_path / "apps" / "sacrifice"
    apps.mkdir(parents=True)
    (apps / "config.yaml").write_text("name: sacrifice\nrepo: o/r\n", encoding="utf-8")
    (tmp_path / "factory_settings.yaml").write_text(
        "modes:\n  default: normal\n  available: [normal, fix-only, paused]\n", encoding="utf-8"
    )
    (tmp_path / "state").mkdir()

    # Use auto_merge_tick in dry-run with a passing fixture so a real
    # ``merge_actions`` row is written.
    story = StoryRecord(
        direction_id="002",
        app="sacrifice",
        title="t",
        slug="s",
        scope="backend",
        state=StoryState.PR_OPEN.value,
        test_plan_json=json.dumps({"test_plan": [{"name": "test_a", "key_steps": ["x"]}]}),
        tech_writer_result_json=json.dumps({"context_updates": [{"path": "context/project.md"}]}),
        github_pr_number=42,
    )
    fixture = FixturePR(
        pr_number=42,
        head_sha="deadbeef",
        base_branch="main",
        labels=[],
        files_changed=["src/foo.py"],
        ci_state="success",
        story=story,
    )
    actions = auto_merge_tick(tmp_path, "sacrifice", dry_run=True, fixture_prs=[fixture])
    assert actions[0].merged, "fixture setup: expected merge to succeed"
    # Reload settings so the test sees the freshly-written YAML.
    from factory.settings.loader import reload_settings

    reload_settings(tmp_path)
    return tmp_path


def test_red_main_ci_triggers_revert(factory_root_with_recent_merge: Path) -> None:
    """Dry-run case: action carries the would-be mode but factory state is NOT mutated."""
    root = factory_root_with_recent_merge
    mode_before = get_mode(root)
    actions = rollback_watch_tick(
        root,
        "sacrifice",
        dry_run=True,
        fixture_ci_state_by_pr={42: "failure"},
        fixture_failing_tests_by_pr={42: ["tests/test_foo.py::test_bar"]},
    )
    assert len(actions) == 1
    a = actions[0]
    assert a.action_type == "revert"
    assert a.revert_pr_number is not None
    assert a.regression_issue_number is not None
    assert a.failing_tests == ["tests/test_foo.py::test_bar"]
    # The RollbackAction reports the would-be mode after rollback...
    assert a.mode_after == "fix-only"
    # ...but in dry-run the actual factory mode must NOT be mutated. The
    # P5.0 cleanup wraps set_mode() in `if not dry_run`; verify by reading
    # the live mode and asserting it's unchanged from before the tick.
    assert get_mode(root) == mode_before


def test_green_main_ci_no_op(factory_root_with_recent_merge: Path) -> None:
    root = factory_root_with_recent_merge
    actions = rollback_watch_tick(
        root,
        "sacrifice",
        dry_run=True,
        fixture_ci_state_by_pr={42: "success"},
    )
    assert len(actions) == 1
    a = actions[0]
    assert a.action_type == "no_op"
    assert a.revert_pr_number is None
    # Mode unchanged.
    assert get_mode(root) == "normal"


def test_pending_main_ci_no_op(factory_root_with_recent_merge: Path) -> None:
    root = factory_root_with_recent_merge
    actions = rollback_watch_tick(
        root,
        "sacrifice",
        dry_run=True,
        fixture_ci_state_by_pr={42: "pending"},
    )
    assert actions[0].action_type == "no_op"
    assert get_mode(root) == "normal"


def test_rollback_recorded_in_db(factory_root_with_recent_merge: Path) -> None:
    root = factory_root_with_recent_merge
    rollback_watch_tick(
        root,
        "sacrifice",
        dry_run=True,
        fixture_ci_state_by_pr={42: "failure"},
        fixture_failing_tests_by_pr={42: ["x::y"]},
    )
    db = root / "state" / "factory.db"
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        rows = session.exec(select(RollbackActionRecord)).all()
    assert len(rows) == 1
    assert rows[0].action_type == "revert"
    assert rows[0].mode_after == "fix-only"


def test_merges_outside_window_are_ignored(factory_root_with_recent_merge: Path) -> None:
    """A merge older than ``window_minutes`` is not re-evaluated."""
    root = factory_root_with_recent_merge
    db = root / "state" / "factory.db"
    # Backdate the existing merge_actions row by 30 min.
    eng = create_engine(f"sqlite:///{db}", echo=False)
    backdate = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    with Session(eng) as session:
        rows = session.exec(select(MergeActionRecord)).all()
        for r in rows:
            r.ts = backdate
            session.add(r)
        session.commit()

    actions = rollback_watch_tick(
        root,
        "sacrifice",
        dry_run=True,
        window_minutes=15,
        fixture_ci_state_by_pr={42: "failure"},
    )
    assert actions == [], f"expected no actions for a merge older than window, got {actions!r}"


def test_real_run_captures_revert_pr_number_from_gh(
    factory_root_with_recent_merge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P5.0 MEDIUM-2: real-run path must capture the new revert PR's number
    from `gh pr revert`'s stdout. We mock the gh CLI binary to return a
    --json payload, mock the GH client for the commit-status read and the
    create_issue call, and assert ``revert_pr_number`` is populated.
    """
    import subprocess as _sp_mod

    from factory.chain import rollback as rb_mod

    root = factory_root_with_recent_merge

    # --- Mock GH client ---
    class _Status:
        state = "failure"

    class _Commit:
        def get_combined_status(self) -> Any:  # noqa: ANN401
            return _Status()

    class _Issue:
        number = 7777

    class _Repo:
        def get_commit(self, _sha: str) -> _Commit:
            return _Commit()

        def create_issue(self, **_kwargs: Any) -> _Issue:  # noqa: ANN401
            return _Issue()

    class _Client:
        def get_repo(self, _repo: str) -> _Repo:
            return _Repo()

    # --- Mock subprocess.run to simulate `gh pr revert --json url,number`. ---
    captured_args: list[list[str]] = []

    class _Proc:
        def __init__(self, stdout: str, stderr: str = "") -> None:
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = 0

    def _fake_run(argv: list[str], **_kwargs: Any) -> _Proc:  # noqa: ANN401
        captured_args.append(argv)
        # gh returns JSON when --json flag is honored.
        return _Proc(stdout='{"url":"https://github.com/xvanov/sacrifice/pull/4242","number":4242}')

    monkeypatch.setattr(_sp_mod, "run", _fake_run)

    actions = rb_mod.rollback_watch_tick(
        root,
        "sacrifice",
        dry_run=False,
        github_client=_Client(),
    )
    assert len(actions) == 1
    a = actions[0]
    assert a.action_type == "revert"
    assert a.revert_pr_number == 4242, (
        f"expected revert_pr_number captured from gh stdout JSON, got {a.revert_pr_number!r}"
    )
    assert a.regression_issue_number == 7777
    # gh was invoked with --json url,number.
    assert any("--json" in argv for argv in captured_args), captured_args


def test_real_run_falls_back_to_url_parse_when_json_missing(
    factory_root_with_recent_merge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the installed gh CLI doesn't support --json on pr revert, the
    rollback worker must still recover the revert PR number by parsing the
    /pull/N pattern from stdout/stderr."""
    import subprocess as _sp_mod

    from factory.chain import rollback as rb_mod

    root = factory_root_with_recent_merge

    class _Status:
        state = "failure"

    class _Commit:
        def get_combined_status(self) -> Any:  # noqa: ANN401
            return _Status()

    class _Issue:
        number = 7778

    class _Repo:
        def get_commit(self, _sha: str) -> _Commit:
            return _Commit()

        def create_issue(self, **_kwargs: Any) -> _Issue:  # noqa: ANN401
            return _Issue()

    class _Client:
        def get_repo(self, _repo: str) -> _Repo:
            return _Repo()

    class _Proc:
        def __init__(self) -> None:
            # No JSON; just a human-readable URL line as older gh prints.
            self.stdout = "https://github.com/xvanov/sacrifice/pull/5151\n"
            self.stderr = ""
            self.returncode = 0

    def _fake_run(_argv: list[str], **_kwargs: Any) -> _Proc:  # noqa: ANN401
        return _Proc()

    monkeypatch.setattr(_sp_mod, "run", _fake_run)

    actions = rb_mod.rollback_watch_tick(
        root,
        "sacrifice",
        dry_run=False,
        github_client=_Client(),
    )
    assert actions[0].revert_pr_number == 5151
