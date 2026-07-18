"""Tests for factory.manager.recovery — the operational self-healing layer.

Every ``gh``/git call is mocked (no network). Each playbook gets: a
precondition-match test (action taken, mutation asserted), one test per
failing sub-condition (no action), a dry-run test (no mutation), and
cooldown/cap anti-thrash tests. Playbook 4 (escalate-only) gets a detection
test. ``run_recovery_cycle`` gets an end-to-end wiring test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, create_engine

from factory.chain.state_machine import StoryRecord, StoryState
from factory.manager import recovery

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir()
    (tmp_path / "apps").mkdir()
    return tmp_path


def _write_app_config(
    root: Path,
    app: str,
    *,
    repo: str = "o/r",
    deploy_enabled: bool = False,
    pre_deploy_commands: list[str] | None = None,
    app_repo_path: str = "app_repo",
    extra_yaml: str = "",
) -> Path:
    app_dir = root / "apps" / app
    app_dir.mkdir(parents=True, exist_ok=True)
    cmds = pre_deploy_commands if pre_deploy_commands is not None else [
        "docker compose -f docker-compose.prod.yml build"
    ]
    cmds_yaml = "\n".join(f'    - "{c}"' for c in cmds) or "    []"
    text = (
        f"name: {app}\n"
        f"repo: {repo}\n"
        f'app_repo_path: "{app_repo_path}"\n'
        "deploy:\n"
        f"  enabled: {'true' if deploy_enabled else 'false'}\n"
        "  pre_deploy_commands:\n"
        f"{cmds_yaml}\n"
        f"{extra_yaml}"
    )
    cfg_path = app_dir / "config.yaml"
    cfg_path.write_text(text, encoding="utf-8")
    return cfg_path


def _db_path(root: Path) -> Path:
    return root / "state" / "factory.db"


def _engine(root: Path):
    eng = create_engine(f"sqlite:///{_db_path(root)}", echo=False)
    from sqlmodel import SQLModel

    SQLModel.metadata.create_all(eng)
    return eng


def _add_story(root: Path, **kwargs) -> int:
    defaults = dict(
        direction_id="001",
        app="sacrifice",
        title="t",
        slug="s",
        scope="backend",
        state=StoryState.STORY_CREATED.value,
    )
    defaults.update(kwargs)
    eng = _engine(root)
    with Session(eng) as session:
        story = StoryRecord(**defaults)
        session.add(story)
        session.commit()
        session.refresh(story)
        return story.id


def _get_story(root: Path, story_id: int) -> StoryRecord:
    eng = _engine(root)
    with Session(eng) as session:
        story = session.get(StoryRecord, story_id)
        assert story is not None
        session.expunge(story)
        return story


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --------------------------------------------------------------------------- #
# Playbook 1: retry-mergeable-blocked-story
# --------------------------------------------------------------------------- #


class TestRetryMergeableBlocked:
    def test_precondition_matches_and_action_taken(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        story_id = _add_story(
            root,
            state=StoryState.BLOCKED_DEPLOY_FAILED.value,
            github_pr_number=42,
            error="auto-merge gave up: terminally un-mergeable",
        )

        def fake_gh_pr_view(*, repo, pr_number, runner=None):
            assert repo == "o/r"
            assert pr_number == 42
            return {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}

        targets = recovery.detect_retry_mergeable_blocked_stories(
            root, gh_pr_view=fake_gh_pr_view
        )
        assert len(targets) == 1
        target = targets[0]
        assert target.playbook == recovery.PLAYBOOK_RETRY_MERGEABLE_BLOCKED
        assert target.story_id == story_id

        outcome = recovery.execute_retry_mergeable_blocked_story(root, target, dry_run=False)
        assert outcome.status == "recovered"

        story = _get_story(root, story_id)
        assert story.state == StoryState.PR_OPEN.value
        assert story.error is None

    @pytest.mark.parametrize(
        "gh_response",
        [
            {"state": "OPEN", "mergeable": "CONFLICTING"},
            {"state": "CLOSED", "mergeable": "MERGEABLE"},
            {"state": "MERGED", "mergeable": "MERGEABLE"},
        ],
    )
    def test_precondition_fails_on_bad_gh_state(self, root: Path, gh_response: dict) -> None:
        _write_app_config(root, "sacrifice")
        _add_story(root, state=StoryState.BLOCKED_DEPLOY_FAILED.value, github_pr_number=42)

        targets = recovery.detect_retry_mergeable_blocked_stories(
            root, gh_pr_view=lambda **_: gh_response
        )
        assert targets == []

    def test_precondition_fails_when_gh_call_fails(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        _add_story(root, state=StoryState.BLOCKED_DEPLOY_FAILED.value, github_pr_number=42)

        targets = recovery.detect_retry_mergeable_blocked_stories(
            root, gh_pr_view=lambda **_: None
        )
        assert targets == []

    def test_precondition_fails_when_no_pr_number(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        _add_story(root, state=StoryState.BLOCKED_DEPLOY_FAILED.value, github_pr_number=None)

        targets = recovery.detect_retry_mergeable_blocked_stories(
            root, gh_pr_view=lambda **_: {"state": "OPEN", "mergeable": "MERGEABLE"}
        )
        assert targets == []

    def test_precondition_fails_when_state_not_blocked(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        _add_story(root, state=StoryState.PR_OPEN.value, github_pr_number=42)

        targets = recovery.detect_retry_mergeable_blocked_stories(
            root, gh_pr_view=lambda **_: {"state": "OPEN", "mergeable": "MERGEABLE"}
        )
        assert targets == []

    def test_dry_run_makes_no_mutation(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        story_id = _add_story(
            root, state=StoryState.BLOCKED_DEPLOY_FAILED.value, github_pr_number=42, error="boom"
        )
        target = recovery.RecoveryTarget(
            playbook=recovery.PLAYBOOK_RETRY_MERGEABLE_BLOCKED,
            key=f"story:{story_id}",
            description="x",
            story_id=story_id,
            app="sacrifice",
        )
        outcome = recovery.execute_retry_mergeable_blocked_story(root, target, dry_run=True)
        assert outcome.status == "dry_run"

        story = _get_story(root, story_id)
        assert story.state == StoryState.BLOCKED_DEPLOY_FAILED.value
        assert story.error == "boom"


# --------------------------------------------------------------------------- #
# Playbook 2: redispatch-phantom-pr-open
# --------------------------------------------------------------------------- #


class TestRedispatchPhantomPr:
    def test_precondition_matches_and_action_taken(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        now = datetime.now(UTC)
        old = now - timedelta(minutes=45)
        story_id = _add_story(
            root,
            state=StoryState.PR_OPEN.value,
            github_pr_number=None,
            github_branch="story/42-foo",
            updated_at=_iso(old),
            error="dispatch died",
        )

        targets = recovery.detect_phantom_pr_open_stories(
            root, now=now, gh_branch_exists=lambda **_: False
        )
        assert len(targets) == 1
        outcome = recovery.execute_redispatch_phantom_pr(root, targets[0], dry_run=False)
        assert outcome.status == "recovered"

        story = _get_story(root, story_id)
        assert story.state == StoryState.STORY_CREATED.value
        assert story.github_pr_number is None
        assert story.github_branch is None
        assert story.error is None

    def test_precondition_fails_when_branch_exists(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        now = datetime.now(UTC)
        old = now - timedelta(minutes=45)
        _add_story(
            root,
            state=StoryState.PR_OPEN.value,
            github_pr_number=None,
            github_branch="story/42-foo",
            updated_at=_iso(old),
        )
        targets = recovery.detect_phantom_pr_open_stories(
            root, now=now, gh_branch_exists=lambda **_: True
        )
        assert targets == []

    def test_precondition_fails_when_gh_uncertain(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        now = datetime.now(UTC)
        old = now - timedelta(minutes=45)
        _add_story(
            root,
            state=StoryState.PR_OPEN.value,
            github_pr_number=None,
            github_branch="story/42-foo",
            updated_at=_iso(old),
        )
        targets = recovery.detect_phantom_pr_open_stories(
            root, now=now, gh_branch_exists=lambda **_: None
        )
        assert targets == []

    def test_precondition_fails_when_too_fresh(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        now = datetime.now(UTC)
        fresh = now - timedelta(minutes=2)
        _add_story(
            root,
            state=StoryState.PR_OPEN.value,
            github_pr_number=None,
            github_branch="story/42-foo",
            updated_at=_iso(fresh),
        )
        targets = recovery.detect_phantom_pr_open_stories(
            root, now=now, gh_branch_exists=lambda **_: False
        )
        assert targets == []

    def test_precondition_fails_when_has_pr_number(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        now = datetime.now(UTC)
        old = now - timedelta(minutes=45)
        _add_story(
            root,
            state=StoryState.PR_OPEN.value,
            github_pr_number=7,
            github_branch="story/42-foo",
            updated_at=_iso(old),
        )
        targets = recovery.detect_phantom_pr_open_stories(
            root, now=now, gh_branch_exists=lambda **_: False
        )
        assert targets == []

    def test_precondition_fails_when_no_branch(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        now = datetime.now(UTC)
        old = now - timedelta(minutes=45)
        _add_story(
            root,
            state=StoryState.PR_OPEN.value,
            github_pr_number=None,
            github_branch=None,
            updated_at=_iso(old),
        )
        targets = recovery.detect_phantom_pr_open_stories(
            root, now=now, gh_branch_exists=lambda **_: False
        )
        assert targets == []

    def test_dry_run_makes_no_mutation(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        story_id = _add_story(
            root,
            state=StoryState.PR_OPEN.value,
            github_pr_number=None,
            github_branch="story/42-foo",
            error="dispatch died",
        )
        target = recovery.RecoveryTarget(
            playbook=recovery.PLAYBOOK_REDISPATCH_PHANTOM_PR,
            key=f"story:{story_id}",
            description="x",
            story_id=story_id,
            app="sacrifice",
        )
        outcome = recovery.execute_redispatch_phantom_pr(root, target, dry_run=True)
        assert outcome.status == "dry_run"

        story = _get_story(root, story_id)
        assert story.state == StoryState.PR_OPEN.value
        assert story.github_branch == "story/42-foo"
        assert story.error == "dispatch died"


# --------------------------------------------------------------------------- #
# Playbook 3: revert-premature-deploy-enable
# --------------------------------------------------------------------------- #


class TestRevertPrematureDeployEnable:
    def test_precondition_matches_and_action_taken(self, root: Path) -> None:
        _write_app_config(root, "sacrifice", deploy_enabled=True)
        # app_repo/docker-compose.prod.yml deliberately absent.
        targets = recovery.detect_premature_deploy_enabled(root)
        assert len(targets) == 1
        target = targets[0]
        assert target.playbook == recovery.PLAYBOOK_REVERT_PREMATURE_DEPLOY
        assert target.app == "sacrifice"

        outcome = recovery.execute_revert_premature_deploy_enable(root, target, dry_run=False)
        assert outcome.status == "recovered"

        text = (root / "apps" / "sacrifice" / "config.yaml").read_text()
        assert "enabled: false" in text
        # pre_deploy_commands untouched.
        assert "docker-compose.prod.yml" in text

    def test_precondition_fails_when_artifact_present(self, root: Path) -> None:
        _write_app_config(root, "sacrifice", deploy_enabled=True)
        app_repo = root / "app_repo"
        app_repo.mkdir(parents=True)
        (app_repo / "docker-compose.prod.yml").write_text("services: {}\n")

        targets = recovery.detect_premature_deploy_enabled(root)
        assert targets == []

    def test_precondition_fails_when_already_disabled(self, root: Path) -> None:
        _write_app_config(root, "sacrifice", deploy_enabled=False)
        targets = recovery.detect_premature_deploy_enabled(root)
        assert targets == []

    def test_precondition_fails_when_no_file_flag(self, root: Path) -> None:
        _write_app_config(
            root, "sacrifice", deploy_enabled=True, pre_deploy_commands=["echo hi"]
        )
        targets = recovery.detect_premature_deploy_enabled(root)
        assert targets == []

    def test_dry_run_makes_no_mutation(self, root: Path) -> None:
        cfg_path = _write_app_config(root, "sacrifice", deploy_enabled=True)
        original = cfg_path.read_text()
        target = recovery.RecoveryTarget(
            playbook=recovery.PLAYBOOK_REVERT_PREMATURE_DEPLOY,
            key="app:sacrifice",
            description="x",
            app="sacrifice",
            extra={"config_path": str(cfg_path)},
        )
        outcome = recovery.execute_revert_premature_deploy_enable(root, target, dry_run=True)
        assert outcome.status == "dry_run"
        assert cfg_path.read_text() == original

    def test_preserves_comments_and_other_keys(self, root: Path) -> None:
        cfg_path = root / "apps" / "myapp" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(
            "name: myapp\n"
            "repo: o/r\n"
            "app_repo_path: \"app_repo\"\n"
            "# Phase 5 deploy block, hand-written comment.\n"
            "deploy:\n"
            "  enabled: true\n"
            "  pre_deploy_commands:\n"
            '    - "docker compose -f docker-compose.prod.yml build"\n'
            "  deploy_command: \"docker compose -f docker-compose.prod.yml up -d\"\n"
            "gates:\n"
            "  lint_command: \"ruff check .\"\n",
            encoding="utf-8",
        )
        text = cfg_path.read_text(encoding="utf-8")
        new_text, changed = recovery._set_deploy_enabled_false(text)
        assert changed
        assert "# Phase 5 deploy block, hand-written comment." in new_text
        assert "enabled: false" in new_text
        assert "deploy_command:" in new_text
        assert "lint_command:" in new_text


# --------------------------------------------------------------------------- #
# Playbook 4: conflicting-gated-pr (escalate-only)
# --------------------------------------------------------------------------- #


class TestConflictingGatedPr:
    def test_precondition_matches_and_escalates(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        story_id = _add_story(
            root, state=StoryState.CI_GREEN.value, github_pr_number=99, github_branch="b"
        )

        targets = recovery.detect_conflicting_gated_prs(
            root, gh_pr_view=lambda **_: {"state": "OPEN", "mergeable": "CONFLICTING"}
        )
        assert len(targets) == 1
        target = targets[0]
        assert target.story_id == story_id
        assert "rebase" in target.extra["recommendation"].lower()
        assert "gh pr checkout 99" in target.extra["recommendation"]

    def test_precondition_fails_when_not_conflicting(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        _add_story(root, state=StoryState.CI_GREEN.value, github_pr_number=99)
        targets = recovery.detect_conflicting_gated_prs(
            root, gh_pr_view=lambda **_: {"state": "OPEN", "mergeable": "MERGEABLE"}
        )
        assert targets == []

    def test_never_mutates_via_run_recovery_cycle(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        story_id = _add_story(root, state=StoryState.CI_GREEN.value, github_pr_number=99)

        summary = recovery.run_recovery_cycle(
            root,
            dry_run=False,
            gh_pr_view=lambda **_: {"state": "OPEN", "mergeable": "CONFLICTING"},
        )
        assert len(summary["escalated"]) == 1
        assert summary["escalated"][0]["playbook"] == recovery.PLAYBOOK_CONFLICTING_GATED_PR

        story = _get_story(root, story_id)
        assert story.state == StoryState.CI_GREEN.value  # untouched


# --------------------------------------------------------------------------- #
# Unmatched -> nothing happens (falls through to existing escalate path)
# --------------------------------------------------------------------------- #


def test_no_targets_when_nothing_matches(root: Path) -> None:
    _write_app_config(root, "sacrifice", deploy_enabled=False)
    _add_story(root, state=StoryState.DEV_IN_PROGRESS.value)

    summary = recovery.run_recovery_cycle(
        root,
        dry_run=False,
        gh_pr_view=lambda **_: None,
        gh_branch_exists=lambda **_: None,
    )
    assert summary["recovered"] == []
    assert summary["escalated"] == []
    assert summary["errors"] == []


# --------------------------------------------------------------------------- #
# Anti-thrash: cooldown + per-cycle cap
# --------------------------------------------------------------------------- #


class TestAntiThrash:
    def test_cooldown_blocks_reapplication_and_escalates_instead(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        story_id = _add_story(
            root, state=StoryState.BLOCKED_DEPLOY_FAILED.value, github_pr_number=42
        )
        now = datetime.now(UTC)

        gh = lambda **_: {"state": "OPEN", "mergeable": "MERGEABLE"}  # noqa: E731

        # First cycle: recovers.
        summary1 = recovery.run_recovery_cycle(root, dry_run=False, now=now, gh_pr_view=gh)
        assert len(summary1["recovered"]) == 1

        # Story re-blocks (simulating a re-failure).
        eng = _engine(root)
        with Session(eng) as session:
            story = session.get(StoryRecord, story_id)
            story.state = StoryState.BLOCKED_DEPLOY_FAILED.value
            session.add(story)
            session.commit()

        # Second cycle, 5 minutes later (within the 30-minute cooldown):
        # must NOT re-apply; must escalate instead.
        summary2 = recovery.run_recovery_cycle(
            root, dry_run=False, now=now + timedelta(minutes=5), gh_pr_view=gh
        )
        assert summary2["recovered"] == []
        assert len(summary2["escalated"]) == 1
        assert summary2["escalated"][0]["reason"] == "cooldown"

        story = _get_story(root, story_id)
        assert story.state == StoryState.BLOCKED_DEPLOY_FAILED.value  # untouched this cycle

    def test_cooldown_expires(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        story_id = _add_story(
            root, state=StoryState.BLOCKED_DEPLOY_FAILED.value, github_pr_number=42
        )
        now = datetime.now(UTC)
        gh = lambda **_: {"state": "OPEN", "mergeable": "MERGEABLE"}  # noqa: E731

        recovery.run_recovery_cycle(root, dry_run=False, now=now, gh_pr_view=gh)

        eng = _engine(root)
        with Session(eng) as session:
            story = session.get(StoryRecord, story_id)
            story.state = StoryState.BLOCKED_DEPLOY_FAILED.value
            session.add(story)
            session.commit()

        summary2 = recovery.run_recovery_cycle(
            root, dry_run=False, now=now + timedelta(minutes=45), gh_pr_view=gh
        )
        assert len(summary2["recovered"]) == 1

    def test_per_cycle_cap(self, root: Path) -> None:
        _write_app_config(root, "sacrifice")
        ids = [
            _add_story(root, state=StoryState.BLOCKED_DEPLOY_FAILED.value, github_pr_number=n)
            for n in range(1, 4)
        ]
        gh = lambda **_: {"state": "OPEN", "mergeable": "MERGEABLE"}  # noqa: E731

        summary = recovery.run_recovery_cycle(root, dry_run=False, max_actions=1, gh_pr_view=gh)
        assert len(summary["recovered"]) == 1
        assert len(summary["escalated"]) == 2
        assert all(e["reason"] == "cap" for e in summary["escalated"])

        recovered_states = [_get_story(root, sid).state for sid in ids]
        assert recovered_states.count(StoryState.PR_OPEN.value) == 1
        assert recovered_states.count(StoryState.BLOCKED_DEPLOY_FAILED.value) == 2

    def test_dry_run_cap_does_not_apply(self, root: Path) -> None:
        """In dry-run, the cap should not suppress logging every match --
        no mutation happens anyway, so there's nothing to bound."""
        _write_app_config(root, "sacrifice")
        for n in range(1, 4):
            _add_story(root, state=StoryState.BLOCKED_DEPLOY_FAILED.value, github_pr_number=n)
        gh = lambda **_: {"state": "OPEN", "mergeable": "MERGEABLE"}  # noqa: E731

        summary = recovery.run_recovery_cycle(root, dry_run=True, max_actions=1, gh_pr_view=gh)
        assert len(summary["recovered"]) == 3
        assert all(r.get("dry_run") for r in summary["recovered"])


# --------------------------------------------------------------------------- #
# Recovery log
# --------------------------------------------------------------------------- #


def test_recovery_actions_are_logged(root: Path) -> None:
    _write_app_config(root, "sacrifice")
    _add_story(root, state=StoryState.BLOCKED_DEPLOY_FAILED.value, github_pr_number=42)
    gh = lambda **_: {"state": "OPEN", "mergeable": "MERGEABLE"}  # noqa: E731

    recovery.run_recovery_cycle(root, dry_run=False, gh_pr_view=gh)

    log_path = root / "state" / "events" / "recovery.ndjson"
    assert log_path.exists()
    lines = [line for line in log_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    import json

    rec = json.loads(lines[0])
    assert rec["playbook"] == recovery.PLAYBOOK_RETRY_MERGEABLE_BLOCKED
    assert rec["status"] == "recovered"
    assert "ts" in rec
    assert "precondition_snapshot" in rec


# --------------------------------------------------------------------------- #
# Halt integration
# --------------------------------------------------------------------------- #


def test_halted_forces_dry_run(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_app_config(root, "sacrifice")
    story_id = _add_story(
        root, state=StoryState.BLOCKED_DEPLOY_FAILED.value, github_pr_number=42
    )
    gh = lambda **_: {"state": "OPEN", "mergeable": "MERGEABLE"}  # noqa: E731

    import factory.manager.halt as halt_mod

    monkeypatch.setattr(halt_mod, "is_halted", lambda *, root: True)

    summary = recovery.run_recovery_cycle(root, dry_run=False, gh_pr_view=gh)
    assert summary["dry_run"] is True
    assert summary["forced_dry_run_halted"] is True

    story = _get_story(root, story_id)
    assert story.state == StoryState.BLOCKED_DEPLOY_FAILED.value  # untouched


# --------------------------------------------------------------------------- #
# gh/git wrapper resilience
# --------------------------------------------------------------------------- #


def test_gh_pr_view_returns_none_on_transient_failure() -> None:
    def _raising_runner(*_args, **_kwargs):
        raise TimeoutError("boom")

    result = recovery._gh_pr_view(repo="o/r", pr_number=1, runner=_raising_runner)
    assert result is None


def test_gh_branch_exists_returns_false_on_404() -> None:
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "gh: Branch not found (HTTP 404)"

    result = recovery._gh_branch_exists(
        repo="o/r", branch="b", runner=lambda *a, **k: _Proc()
    )
    assert result is False


def test_gh_branch_exists_returns_none_on_uncertain_error() -> None:
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "gh: authentication required"

    result = recovery._gh_branch_exists(
        repo="o/r", branch="b", runner=lambda *a, **k: _Proc()
    )
    assert result is None
