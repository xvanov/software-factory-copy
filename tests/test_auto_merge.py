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
        tech_writer_result_json=json.dumps({"context_updates": [{"path": "context/project.md"}]}),
        github_pr_number=42,
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


# --------------------------------------------------------------------------- #
# Dual-draft sibling cleanup wiring (audit 2026-07-18, leak 4 of 4)
# --------------------------------------------------------------------------- #


def test_auto_merge_closes_sibling_draft_alternative_on_merge(
    factory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a dual-draft story's PR merges (real-run, not dry-run), the
    losing sibling draft-alternative's still-open GitHub issue gets closed
    automatically — the cleanup the tracker comment promised but that never
    actually ran (e.g. #210 stayed open forever after #209 merged)."""
    import subprocess

    from factory.chain.handlers import persist_story

    db = factory_root / "state" / "factory.db"

    winner = StoryRecord(
        direction_id="007",
        app="sacrifice",
        title="Make it better — narrow read",
        slug="make-it-better-alt-a",
        scope="backend",
        state=StoryState.PR_OPEN.value,
        chain_kind="docs",
        github_issue_number=209,
        github_pr_number=555,
    )
    persist_story(winner, db)
    loser = StoryRecord(
        direction_id="007",
        app="sacrifice",
        title="Make it better — broad read",
        slug="make-it-better-alt-b",
        scope="backend",
        state=StoryState.PR_OPEN.value,
        chain_kind="docs",
        github_issue_number=210,
        github_pr_number=556,
    )
    persist_story(loser, db)

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run, raising=True)

    class _Issue:
        def __init__(self, number: int, state: str = "open") -> None:
            self.number = number
            self.state = state
            self.comments: list[str] = []
            self.close_reason: str | None = None

        def create_comment(self, body: str) -> None:
            self.comments.append(body)

        def edit(self, *, state: str, state_reason: str | None = None) -> None:
            self.state = state
            self.close_reason = state_reason

    class _Repo:
        def __init__(self, issues: dict[int, _Issue]) -> None:
            self._issues = issues

        def get_issue(self, n: int) -> _Issue:
            return self._issues[n]

    class _Client:
        def __init__(self, repo: _Repo) -> None:
            self._repo = repo

        def get_repo(self, full_name: str) -> _Repo:
            return self._repo

    sibling_issue = _Issue(210)
    client = _Client(_Repo({209: _Issue(209), 210: sibling_issue}))

    fixture = FixturePR(
        pr_number=555,
        head_sha="alt-a-sha",
        base_branch="main",
        labels=[],
        files_changed=["context/project.md"],
        ci_state="success",
        story=winner,
    )

    actions = auto_merge_tick(
        factory_root,
        "sacrifice",
        dry_run=False,
        fixture_prs=[fixture],
        github_client=client,
        db_path=db,
        # Real-run now CONFIRMS the merge on GitHub before claiming merged=True
        # (``--auto`` only enables async auto-merge). This PR's checks were
        # already green, so the confirmation query reports it merged.
        pr_merged_query=lambda **_kwargs: True,
    )

    assert actions[0].merged, actions[0].reason
    assert sibling_issue.state == "closed"
    assert sibling_issue.close_reason == "not_planned"
    assert sibling_issue.comments and "#209" in sibling_issue.comments[0]


def test_auto_merge_does_not_close_sibling_when_dry_run(
    factory_root: Path,
) -> None:
    """Sanity: dry-run merges must never touch GitHub for the sibling
    cleanup either (mirrors the rest of the worker's dry-run contract)."""
    from factory.chain.handlers import persist_story

    db = factory_root / "state" / "factory.db"
    winner = StoryRecord(
        direction_id="011",
        app="sacrifice",
        title="Make it better — narrow read",
        slug="make-it-better-alt-a",
        scope="docs",
        state=StoryState.PR_OPEN.value,
        chain_kind="docs",
        github_issue_number=219,
        github_pr_number=565,
    )
    persist_story(winner, db)
    loser = StoryRecord(
        direction_id="011",
        app="sacrifice",
        title="Make it better — broad read",
        slug="make-it-better-alt-b",
        scope="docs",
        state=StoryState.PR_OPEN.value,
        chain_kind="docs",
        github_issue_number=220,
        github_pr_number=566,
    )
    persist_story(loser, db)

    fixture = FixturePR(
        pr_number=565,
        head_sha="alt-a-sha-2",
        base_branch="main",
        labels=[],
        files_changed=["context/project.md"],
        ci_state="success",
        story=winner,
    )
    actions = auto_merge_tick(
        factory_root, "sacrifice", dry_run=True, fixture_prs=[fixture], db_path=db,
    )
    assert actions[0].merged, actions[0].reason
    # No github_client was even provided in dry-run; nothing to assert on
    # the (nonexistent) sibling issue beyond "no exception raised".


# --------------------------------------------------------------------------- #
# merged != auto-merge-enabled: ``gh pr merge --auto`` only ENABLES GitHub
# auto-merge; it does NOT merge now. ``merged=True`` must reflect a REAL merge.
# --------------------------------------------------------------------------- #


def _docs_pr_story(*, pr_number: int, state: str = StoryState.PR_OPEN.value) -> StoryRecord:
    """A docs-chain story so real-run gate evaluation is hermetic (the docs
    chain synthesizes ``canonical-paths-only`` — no gate command shell-outs)."""
    return StoryRecord(
        direction_id="030",
        app="sacrifice",
        title="t",
        slug=f"amerge-{pr_number}",
        scope="docs",
        state=state,
        chain_kind="docs",
        github_issue_number=pr_number,
        github_pr_number=pr_number,
    )


def _merged_rows(db: Path) -> list[MergeActionRecord]:
    with Session(create_engine(f"sqlite:///{db}")) as ses:
        return list(
            ses.exec(select(MergeActionRecord).where(MergeActionRecord.merged == True))  # noqa: E712
        )


def _reload_story(db: Path, story_id: int | None) -> StoryRecord:
    with Session(create_engine(f"sqlite:///{db}")) as ses:
        return ses.exec(select(StoryRecord).where(StoryRecord.id == story_id)).one()


def test_auto_merge_enabled_but_not_merged_does_not_advance_or_record(
    factory_root: Path,
) -> None:
    """The strand root cause: ``gh pr merge --auto`` succeeded (auto-merge
    ENABLED) but the PR is not merged yet (required checks pending). The worker
    must NOT claim merged=True, NOT record a merged merge-action, and NOT
    advance the story — it stays in a mergeable state so reconcile + the
    CI-failure loop keep watching it."""
    from factory.chain.handlers import persist_story

    db = factory_root / "state" / "factory.db"
    story = persist_story(_docs_pr_story(pr_number=801), db)

    # Fake gh merge: "enables auto-merge" — returns success (None) WITHOUT
    # merging. Records that it was invoked.
    called: list[bool] = []

    def _fake_merge(**_kwargs: object) -> str | None:
        called.append(True)
        return None

    # PR state query: the PR is still OPEN / not merged.
    def _not_merged(**_kwargs: object) -> bool:
        return False

    fixture = FixturePR(
        pr_number=801,
        head_sha="pending-sha",
        base_branch="main",
        labels=[],
        files_changed=["context/project.md"],
        ci_state="success",
        story=story,
    )

    actions = auto_merge_tick(
        factory_root,
        "sacrifice",
        dry_run=False,
        fixture_prs=[fixture],
        db_path=db,
        merge_fn=_fake_merge,
        pr_merged_query=_not_merged,
    )

    assert called  # the merge (auto-merge enable) was actually attempted
    assert len(actions) == 1
    act = actions[0]
    assert act.merged is False
    assert act.auto_merge_enabled is True
    assert "awaiting required checks" in act.reason
    # No merged=True row → _latest_undeployed_sha never picks it up (no deploy).
    assert _merged_rows(db) == []
    # Story is NOT advanced — stays in a mergeable state (reconcile + CI-failure
    # loop keep watching it); it is NOT stranded at deploy_pending.
    assert _reload_story(db, story.id).state == StoryState.PR_OPEN.value


def test_auto_merge_confirmed_merge_advances_and_enqueues_deploy(
    factory_root: Path,
) -> None:
    """When the post-merge GitHub query confirms the PR ACTUALLY merged (e.g.
    ``--auto`` merged immediately because checks were already green), the worker
    claims merged=True, records a merged merge-action, advances the story to
    DEPLOY_PENDING, and enqueues a deploy — exactly as before."""
    from factory.chain.handlers import persist_story
    from factory.deploy.models import DeployQueueEntry

    db = factory_root / "state" / "factory.db"
    story = persist_story(_docs_pr_story(pr_number=802), db)

    # Start query returns False (not yet merged at the top of _evaluate_one_pr),
    # post-merge query returns True (the --auto merge landed). Stateful by count.
    calls: list[int] = []

    def _merged_after_merge(**_kwargs: object) -> bool:
        calls.append(1)
        return len(calls) >= 2  # 1st call (start short-circuit) False, 2nd True

    def _fake_merge(**_kwargs: object) -> str | None:
        return None  # success (merge requested/performed)

    fixture = FixturePR(
        pr_number=802,
        head_sha="merged-sha",
        base_branch="main",
        labels=[],
        files_changed=["context/project.md"],
        ci_state="success",
        story=story,
    )

    actions = auto_merge_tick(
        factory_root,
        "sacrifice",
        dry_run=False,
        fixture_prs=[fixture],
        db_path=db,
        merge_fn=_fake_merge,
        pr_merged_query=_merged_after_merge,
    )

    assert actions[0].merged is True
    assert actions[0].auto_merge_enabled is False
    # Merged row recorded for the head sha → deploy pipeline can pick it up.
    merged = _merged_rows(db)
    assert [r.head_sha for r in merged] == ["merged-sha"]
    # Story advanced to DEPLOY_PENDING.
    assert _reload_story(db, story.id).state == StoryState.DEPLOY_PENDING.value
    # Deploy enqueued for the merged sha.
    with Session(create_engine(f"sqlite:///{db}")) as ses:
        q = list(ses.exec(select(DeployQueueEntry).where(DeployQueueEntry.sha == "merged-sha")))
    assert len(q) == 1


def test_auto_merge_already_merged_short_circuits(factory_root: Path) -> None:
    """If the PR is ALREADY merged on GitHub at the top of the tick (the async
    ``--auto`` merge landed between ticks), the worker short-circuits to
    merged=True without re-running gates/staging, and drives deploy."""
    from factory.chain.handlers import persist_story

    db = factory_root / "state" / "factory.db"
    story = persist_story(_docs_pr_story(pr_number=803), db)

    def _already(**_kwargs: object) -> bool:
        return True

    def _fake_merge(**_kwargs: object) -> str | None:  # must NOT be called
        raise AssertionError("merge should be short-circuited when already merged")

    fixture = FixturePR(
        pr_number=803,
        head_sha="landed-sha",
        base_branch="main",
        labels=[],
        files_changed=["context/project.md"],
        ci_state="success",
        story=story,
    )

    actions = auto_merge_tick(
        factory_root,
        "sacrifice",
        dry_run=False,
        fixture_prs=[fixture],
        db_path=db,
        merge_fn=_fake_merge,
        pr_merged_query=_already,
    )

    assert actions[0].merged is True
    assert actions[0].reason == "already merged on GitHub"
    assert _reload_story(db, story.id).state == StoryState.DEPLOY_PENDING.value


def test_auto_merge_enabled_then_failing_check_leaves_story_redispatchable(
    factory_root: Path,
) -> None:
    """Regression for the exact strand (factory story 102 / PR #57): auto-merge
    was ENABLED, then a required check (ruff lint) FAILED, so the PR never
    merges. The story must remain in ``_MERGEABLE_STATES`` so a later tick's
    CI-failure path (``_handle_ci_failure``, guarded to those states) can
    re-dispatch dev — NOT stranded at deploy_pending where nothing watches it."""
    from factory.chain.auto_merge import _MERGEABLE_STATES
    from factory.chain.handlers import persist_story

    db = factory_root / "state" / "factory.db"
    story = persist_story(_docs_pr_story(pr_number=804), db)

    # Tick 1: auto-merge enabled, PR not merged yet.
    fixture = FixturePR(
        pr_number=804,
        head_sha="strand-sha",
        base_branch="main",
        labels=[],
        files_changed=["context/project.md"],
        ci_state="success",
        story=story,
    )
    auto_merge_tick(
        factory_root, "sacrifice", dry_run=False, fixture_prs=[fixture], db_path=db,
        merge_fn=lambda **_k: None,
        pr_merged_query=lambda **_k: False,
    )

    reloaded = _reload_story(db, story.id)
    # The story is still in a mergeable state — reachable by _handle_ci_failure.
    assert reloaded.state in _MERGEABLE_STATES
    assert reloaded.state == StoryState.PR_OPEN.value
    assert _merged_rows(db) == []
