"""Tests for the auto-merge worker.

Driven entirely in dry-run mode with fixture PRs so no network calls
escape the process.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session, create_engine, select

from factory.chain.auto_merge import (
    ALL_GATE_LABELS,
    FixturePR,
    MergeActionRecord,
    auto_merge_tick,
)
from factory.chain.state_machine import StoryRecord, StoryState


@pytest.fixture
def factory_root(tmp_path: Path) -> Path:
    apps = tmp_path / "apps" / "sacrifice"
    apps.mkdir(parents=True)
    (apps / "config.yaml").write_text(
        "name: sacrifice\nrepo: o/r\n"
        "gates:\n"
        "  lint_command: 'ruff check .'\n"
        "  format_check_command: 'ruff format --check .'\n"
        "  type_check_command: 'mypy .'\n"
        "  coverage_command: 'pytest --cov-fail-under=70'\n",
        encoding="utf-8",
    )
    (tmp_path / "state").mkdir()
    return tmp_path


def _good_story(*, state: str = StoryState.PR_OPEN.value) -> StoryRecord:
    return StoryRecord(
        direction_id="002",
        app="sacrifice",
        title="t",
        slug="s",
        scope="backend",
        state=state,
        test_plan_json=json.dumps(
            {
                "test_plan": [
                    {
                        "name": "test_pledge_button",
                        "what_it_asserts": "User pledge dollars flow stores amount",
                        "why_meaningful": "Real outcome — user pledge flow",
                        "key_steps": ["arrange", "act", "assert"],
                    }
                ]
            }
        ),
        test_implementer_result_json=json.dumps({"exit_code": 1, "slop_detected": False}),
        tech_writer_result_json=json.dumps({"context_updates": [{"path": "context/project.md"}]}),
        github_pr_number=42,
        # Phase 8 cleanup: dry-run lint/format/types/coverage gates now require
        # an explicit recorded outcome.
        lint_passed=True,
        format_passed=True,
        types_passed=True,
        coverage_passed=True,
    )


def _good_fixture(*, pr_number: int = 42, labels: list[str] | None = None) -> FixturePR:
    return FixturePR(
        pr_number=pr_number,
        head_sha="deadbeef",
        base_branch="main",
        labels=list(labels or []),
        files_changed=["src/foo.py", "tests/test_foo.py"],
        ci_state="success",
        story=_good_story(),
    )


def test_all_gates_pass_yields_merge(factory_root: Path) -> None:
    pr = _good_fixture()
    actions = auto_merge_tick(factory_root, "sacrifice", dry_run=True, fixture_prs=[pr])
    assert len(actions) == 1
    assert actions[0].merged, actions[0].reason
    assert "all required gates" in actions[0].reason
    assert set(actions[0].gates_passed) == set(ALL_GATE_LABELS)


def test_blocking_label_prevents_merge(factory_root: Path) -> None:
    pr = _good_fixture(labels=["tests-slop"])
    actions = auto_merge_tick(factory_root, "sacrifice", dry_run=True, fixture_prs=[pr])
    assert not actions[0].merged
    assert "blocking labels" in actions[0].reason
    assert "tests-slop" in actions[0].blocking_labels


def test_do_not_merge_label_blocks(factory_root: Path) -> None:
    pr = _good_fixture(labels=["do-not-merge"])
    actions = auto_merge_tick(factory_root, "sacrifice", dry_run=True, fixture_prs=[pr])
    assert not actions[0].merged
    assert "do-not-merge" in actions[0].blocking_labels


def test_needs_test_quality_fix_blocks(factory_root: Path) -> None:
    pr = _good_fixture(labels=["needs-test-quality-fix"])
    actions = auto_merge_tick(factory_root, "sacrifice", dry_run=True, fixture_prs=[pr])
    assert not actions[0].merged


def test_missing_gate_blocks_merge(factory_root: Path) -> None:
    """If any gate would not pass, the missing-label list reflects it."""
    story = _good_story()
    # Wipe the tech_writer record so docs-current fails.
    story.tech_writer_result_json = None
    fixture = FixturePR(
        pr_number=43,
        head_sha="cafe",
        base_branch="main",
        labels=[],
        files_changed=["src/foo.py"],
        ci_state="success",
        story=story,
    )
    actions = auto_merge_tick(factory_root, "sacrifice", dry_run=True, fixture_prs=[fixture])
    assert not actions[0].merged
    assert "missing gate labels" in actions[0].reason
    assert "docs-current" in actions[0].reason


def test_story_state_guard_prevents_premature_merge(factory_root: Path) -> None:
    """A story still in DEV_IN_PROGRESS is not eligible for merge even if
    fixture gates green."""
    story = _good_story(state=StoryState.DEV_IN_PROGRESS.value)
    fixture = FixturePR(
        pr_number=44,
        head_sha="aaaa",
        base_branch="main",
        labels=[],
        files_changed=["src/foo.py"],
        ci_state="success",
        story=story,
    )
    actions = auto_merge_tick(factory_root, "sacrifice", dry_run=True, fixture_prs=[fixture])
    assert not actions[0].merged
    assert "not in mergeable states" in actions[0].reason


def test_merge_action_persisted_in_db(factory_root: Path) -> None:
    """Every evaluation records a row in ``merge_actions`` for the rollback worker."""
    pr = _good_fixture()
    auto_merge_tick(factory_root, "sacrifice", dry_run=True, fixture_prs=[pr])
    db = factory_root / "state" / "factory.db"
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        rows = session.exec(select(MergeActionRecord)).all()
    assert len(rows) == 1
    assert rows[0].pr_number == 42
    assert rows[0].merged is True
    assert "tests-meaningful" in json.loads(rows[0].gates_passed_json)


def test_no_fixtures_no_actions(factory_root: Path) -> None:
    """Dry-run with no PRs returns an empty list, not an error."""
    actions = auto_merge_tick(factory_root, "sacrifice", dry_run=True, fixture_prs=[])
    assert actions == []


# --------------------------------------------------------------------------- #
# Docs-chain auto-merge — the docs chain skips the 10 TDD gates because the
# canonical-paths enforcer already vetted the PR before reaching PR_OPEN.
# --------------------------------------------------------------------------- #


def _docs_story(*, state: str = StoryState.PR_OPEN.value) -> StoryRecord:
    """Minimal docs-chain StoryRecord at ``state`` with no TDD payload."""
    return StoryRecord(
        direction_id="005",
        app="sacrifice",
        title="Bootstrap context",
        slug="bootstrap-ctx",
        scope="docs",
        state=state,
        chain_kind="docs",
        github_pr_number=99,
    )


def test_docs_chain_pr_open_merges_without_tdd_gates(factory_root: Path) -> None:
    """A docs-chain story at PR_OPEN with no TDD gate labels merges; the
    chain enforcer already ran in ``handle_docs_enforcer``."""
    fixture = FixturePR(
        pr_number=99,
        head_sha="docs-sha",
        base_branch="main",
        labels=[],
        files_changed=["context/project.md"],
        ci_state="success",
        story=_docs_story(),
    )
    actions = auto_merge_tick(factory_root, "sacrifice", dry_run=True, fixture_prs=[fixture])
    assert actions[0].merged, actions[0].reason
    assert "docs chain" in actions[0].reason


def test_docs_chain_blocking_label_blocks(factory_root: Path) -> None:
    """A docs-chain story with a blocking label is refused, same as TDD."""
    fixture = FixturePR(
        pr_number=99,
        head_sha="docs-sha",
        base_branch="main",
        labels=["needs-human-verification"],
        files_changed=["context/project.md"],
        ci_state="success",
        story=_docs_story(),
    )
    actions = auto_merge_tick(factory_root, "sacrifice", dry_run=True, fixture_prs=[fixture])
    assert not actions[0].merged
    assert "blocking labels" in actions[0].reason


def test_tdd_chain_still_requires_all_ten_gates(factory_root: Path) -> None:
    """Regression guard: the docs-chain branch must NOT relax TDD gates.
    A TDD story missing one gate is still refused (here we drop the
    tech_writer payload so docs-current fails)."""
    story = _good_story()
    story.tech_writer_result_json = None  # docs-current gate will fail
    fixture = FixturePR(
        pr_number=42,
        head_sha="tdd-sha",
        base_branch="main",
        labels=[],
        files_changed=["src/foo.py"],
        ci_state="success",
        story=story,
    )
    actions = auto_merge_tick(factory_root, "sacrifice", dry_run=True, fixture_prs=[fixture])
    assert not actions[0].merged
    assert "missing gate labels" in actions[0].reason
    assert "docs-current" in actions[0].reason


# --------------------------------------------------------------------------- #
# _attempt_pr_reconcile — safe branch-update (gh pr update-branch) before sink
# --------------------------------------------------------------------------- #


def test_attempt_pr_reconcile_returns_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from factory.app_config import AppConfig
    from factory.chain import auto_merge as am

    calls: dict[str, list] = {}

    def _fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run, raising=True)
    cfg = AppConfig(name="sacrifice", repo="x/sacrifice", default_branch="main")
    assert am._attempt_pr_reconcile(app_config=cfg, pr_number=90) is True
    # Uses gh pr update-branch (a merge, never --force).
    assert calls["cmd"][:3] == ["gh", "pr", "update-branch"]
    assert "90" in calls["cmd"] and "--force" not in calls["cmd"]


def test_attempt_pr_reconcile_returns_false_on_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from factory.app_config import AppConfig
    from factory.chain import auto_merge as am

    def _fake_run(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, "", "merge conflict")

    monkeypatch.setattr(subprocess, "run", _fake_run, raising=True)
    cfg = AppConfig(name="sacrifice", repo="x/sacrifice", default_branch="main")
    assert am._attempt_pr_reconcile(app_config=cfg, pr_number=90) is False


def test_loop4_story_merges_on_surviving_gates(tmp_path) -> None:
    """A Loop-4 story (dev-owns-tests; no test_implementer/test_designer
    payloads, no recorded lint/coverage flags, no labels applied by anyone)
    must be mergeable when the surviving gates pass: tests-green (recorded
    green dev run), tests-meaningful (no slop), docs-current (tech_writer
    result), canonical-paths-only. The historical 10-label requirement
    permanently blocked every Loop-4 merge (PRs 110/111, 2026-06-11)."""
    import json

    from factory.chain.auto_merge import FixturePR, auto_merge_tick
    from factory.chain.state_machine import StoryRecord, StoryState

    root = tmp_path
    (root / "apps" / "sacrifice").mkdir(parents=True, exist_ok=True)
    (root / "apps" / "sacrifice" / "config.yaml").write_text(
        "name: sacrifice\nrepo: x/y\ndefault_branch: main\n", encoding="utf-8"
    )
    db = root / "state" / "factory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    from sqlmodel import SQLModel, create_engine

    SQLModel.metadata.create_all(create_engine(f"sqlite:///{db}"))
    from factory.chain.handlers import persist_story

    story = persist_story(
        StoryRecord(
            direction_id="007", app="sacrifice", title="t", slug="loop4",
            scope="frontend", state=StoryState.PR_OPEN.value, chain_kind="tdd",
            github_pr_number=110,
            test_implementer_result_json=json.dumps({"exit_code": 0}),
            tech_writer_result_json=json.dumps(
                {"context_updates": ["context/modules/frontend.md"], "rationale": "updated"}
            ),
        ),
        db,
    )
    # Record a green dev run shape the tests-green gate reads in dry-run.
    import sqlite3 as _sq

    conn = _sq.connect(str(db))
    conn.execute("UPDATE stories SET dev_attempts_json=? WHERE id=?",
                 (json.dumps([{"test_run_passed": True, "test_output_tail": "ok"}]), story.id))
    conn.commit()
    conn.close()

    fixture = FixturePR(
        pr_number=110, head_sha="abc", base_branch="main", labels=[],
        files_changed=["frontend/services/api.ts"], ci_state="success",
        story=story, repo_root=None,
    )
    actions = auto_merge_tick(
        app="sacrifice", software_factory_root=root, dry_run=True,
        fixture_prs=[fixture], db_path=db,
    )
    assert len(actions) == 1
    act = actions[0]
    assert act.merged, f"expected merge, got reason={act.reason!r}"
