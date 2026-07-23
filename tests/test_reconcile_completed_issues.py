"""Issue-lifecycle auto-close: dual-draft-aware direction completion + backfill.

Regression + feature coverage for the 2026-07-21 issue-lifecycle finding:

  * ``maybe_close_tracker_issue`` required EVERY child story to be ``DEPLOYED``,
    so a dual-draft direction (whose loser lands in ``SUPERSEDED_BY_SIBLING``)
    could NEVER close its tracker → trackers #54/#60/#77/#88 leaked open.
  * There was no way to reconcile issues left open for already-completed work
    (event-driven close on deploy can no-op on the async ``--auto`` path).
    ``reconcile_completed_issues`` is the idempotent, fail-safe backfill.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from factory.app_config import AppConfig, DeployConfig
from factory.chain.state_machine import StoryRecord, StoryState
from factory.directions.tracker_issue import (
    _direction_is_complete,
    maybe_close_tracker_issue,
    reconcile_completed_issues,
)

# ─── fakes ────────────────────────────────────────────────────────────────


class _Issue:
    def __init__(self, number: int, state: str = "open") -> None:
        self.number = number
        self.state = state
        self.comments: list[str] = []
        self.edits: list[dict[str, Any]] = []

    def create_comment(self, body: str) -> None:
        self.comments.append(body)

    def edit(self, state: str | None = None, state_reason: str | None = None, **_: Any) -> None:
        if state is not None:
            self.state = state
        self.edits.append({"state": state, "state_reason": state_reason})


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


def _app_config() -> AppConfig:
    return AppConfig(
        name="factory",
        repo="xvanov/software-factory",
        default_branch="main",
        context_dir="context",
        deploy=DeployConfig(enabled=False),
        models={},
    )


def _story(state: str) -> SimpleNamespace:
    return SimpleNamespace(state=state)


def _make_direction(root: Path, id_slug: str, *, tracker_issue: int | None) -> None:
    base = root / "apps" / "factory" / "directions" / id_slug
    base.mkdir(parents=True)
    fm = {
        "title": id_slug.replace("-", " ").title(),
        "type": "feature",
        "priority": "p2",
        "explore": False,
        "created_at": "2025-01-01T00:00:00+00:00",
    }
    (base / "direction.md").write_text(
        f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n\n"
        f"# {fm['title']}\n\n## Why\n\nReason.\n\n## Acceptance Criteria\n\n- AC1\n",
        encoding="utf-8",
    )
    state: dict[str, Any] = {"status": "pm-validated"}
    if tracker_issue is not None:
        state["tracker_issue"] = tracker_issue
    (base / "state.yaml").write_text(yaml.safe_dump(state), encoding="utf-8")


def _persist(root: Path, **kw: Any) -> None:
    from factory.chain.handlers import persist_story

    persist_story(StoryRecord(app="factory", **kw), root / "state" / "factory.db")


# ─── _direction_is_complete (Fix A predicate) ──────────────────────────────


def test_complete_dual_draft_deployed_plus_superseded() -> None:
    # THE bug: winner deployed, loser superseded → direction IS complete.
    assert _direction_is_complete(
        [_story(StoryState.DEPLOYED.value), _story(StoryState.SUPERSEDED_BY_SIBLING.value)]
    )


def test_complete_all_deployed() -> None:
    assert _direction_is_complete([_story(StoryState.DEPLOYED.value)])


def test_complete_deployed_plus_invalidated_closed() -> None:
    assert _direction_is_complete([_story(StoryState.DEPLOYED.value), _story("closed")])


def test_incomplete_when_work_in_flight() -> None:
    assert not _direction_is_complete(
        [_story(StoryState.DEPLOYED.value), _story(StoryState.PR_OPEN.value)]
    )


def test_incomplete_when_blocked_story_present() -> None:
    # A blocked story is terminal for the chain but UNRESOLVED — keep tracker open.
    assert not _direction_is_complete(
        [_story(StoryState.DEPLOYED.value), _story(StoryState.BLOCKED_DEPLOY_FAILED.value)]
    )


def test_complete_when_fully_abandoned_no_deploy() -> None:
    # Abandoned-direction close (2026-07-23): every child reached a definitively
    # terminal sink and nothing deployed → the direction produced nothing and
    # never will, so it IS complete (tracker closes; stories stay FMS-surfaced).
    assert _direction_is_complete(
        [_story(StoryState.SUPERSEDED_BY_SIBLING.value), _story("closed")]
    )
    # The app-blocked cluster shape: alt-a CI-abandoned + alt-b dependency-dead.
    assert _direction_is_complete(
        [
            _story(StoryState.BLOCKED_CI_UNRESOLVED.value),
            _story(StoryState.BLOCKED_DEPENDENCY_UNMET.value),
        ]
    )


def test_incomplete_when_recoverable_block_present_no_deploy() -> None:
    # A recoverable-pending-human block (NOT in _RESOLVED_STORY_STATES) keeps the
    # tracker open even with no deploy — the work may still be revived. This is
    # the D092/D094/D098 case that must stay open.
    for blocked in (
        StoryState.BLOCKED_DEPLOY_FAILED.value,
        StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value,
        StoryState.BLOCKED_BUDGET_EXCEEDED.value,
    ):
        assert not _direction_is_complete(
            [_story(blocked), _story(StoryState.STORY_CREATED.value)]
        )


def test_incomplete_when_sibling_ci_pending() -> None:
    # Adversarial-review HIGH: CI_PENDING is "terminal-by-omission" in the state
    # machine (no outgoing _TRANSITIONS edge), so an is_terminal-based check
    # wrongly treated a sibling mid-CI as resolved and closed the tracker while
    # work was in flight. The allowlist must reject it.
    assert not _direction_is_complete(
        [_story(StoryState.DEPLOYED.value), _story(StoryState.CI_PENDING.value)]
    )


def test_incomplete_when_sibling_ready_for_merge() -> None:
    assert not _direction_is_complete(
        [_story(StoryState.DEPLOYED.value), _story(StoryState.READY_FOR_MERGE.value)]
    )


def test_incomplete_when_empty() -> None:
    assert not _direction_is_complete([])


# ─── maybe_close_tracker_issue: dual-draft regression ───────────────────────


def test_dual_draft_direction_closes_tracker(tmp_path: Path) -> None:
    _make_direction(tmp_path, "011-gate-ux", tracker_issue=88)
    _persist(
        tmp_path,
        direction_id="011",
        title="winner",
        slug="gate-ux-narrow",
        scope="backend",
        state=StoryState.DEPLOYED.value,
        github_issue_number=89,
    )
    _persist(
        tmp_path,
        direction_id="011",
        title="loser",
        slug="gate-ux-broad-alt-b",
        scope="backend",
        state=StoryState.SUPERSEDED_BY_SIBLING.value,
        github_issue_number=90,
    )
    tracker = _Issue(88)
    client = _Client(_Repo({88: tracker}))
    closed = maybe_close_tracker_issue("011", _app_config(), client, software_factory_root=tmp_path)
    assert closed is True
    assert tracker.state == "closed"


def test_direction_with_blocked_story_keeps_tracker_open(tmp_path: Path) -> None:
    _make_direction(tmp_path, "012-thing", tracker_issue=77)
    _persist(
        tmp_path,
        direction_id="012",
        title="winner",
        slug="thing-a",
        scope="backend",
        state=StoryState.DEPLOYED.value,
        github_issue_number=78,
    )
    _persist(
        tmp_path,
        direction_id="012",
        title="stuck",
        slug="thing-b",
        scope="backend",
        state=StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value,
        github_issue_number=79,
    )
    tracker = _Issue(77)
    client = _Client(_Repo({77: tracker}))
    assert (
        maybe_close_tracker_issue("012", _app_config(), client, software_factory_root=tmp_path)
        is False
    )
    assert tracker.state == "open"


# ─── reconcile_completed_issues (backfill) ──────────────────────────────────


def _seed_backfill_fixture(tmp_path: Path) -> dict[int, _Issue]:
    # D005: complete — deployed winner (#55) + invalidated loser (state closed, no issue).
    _make_direction(tmp_path, "005-redact", tracker_issue=54)
    _persist(
        tmp_path,
        direction_id="005",
        title="winner",
        slug="redact-narrow",
        scope="backend",
        state=StoryState.DEPLOYED.value,
        github_issue_number=55,
    )
    # D011: complete dual-draft — deployed winner (#89) + superseded loser (#90).
    _make_direction(tmp_path, "011-gate", tracker_issue=88)
    _persist(
        tmp_path,
        direction_id="011",
        title="winner",
        slug="gate-narrow",
        scope="backend",
        state=StoryState.DEPLOYED.value,
        github_issue_number=89,
    )
    _persist(
        tmp_path,
        direction_id="011",
        title="loser",
        slug="gate-broad-alt-b",
        scope="backend",
        state=StoryState.SUPERSEDED_BY_SIBLING.value,
        github_issue_number=90,
    )
    # D099: still in flight — pr_open story → tracker must stay OPEN.
    _make_direction(tmp_path, "099-inflight", tracker_issue=294)
    _persist(
        tmp_path,
        direction_id="099",
        title="wip",
        slug="inflight-a",
        scope="backend",
        state=StoryState.PR_OPEN.value,
        github_issue_number=295,
    )
    return {
        54: _Issue(54),
        55: _Issue(55),
        88: _Issue(88),
        89: _Issue(89),
        90: _Issue(90),
        294: _Issue(294),
        295: _Issue(295),
    }


def test_reconcile_dry_run_closes_nothing(tmp_path: Path) -> None:
    issues = _seed_backfill_fixture(tmp_path)
    client = _Client(_Repo(issues))
    report = reconcile_completed_issues(
        _app_config(), client, software_factory_root=tmp_path, dry_run=True
    )
    # Trackers 54 + 88 and story issues 55, 89 (deployed) + 90 (superseded) would close.
    would = {n for _, n, _ in report["would_close"]}
    assert would == {54, 88, 55, 89, 90}
    # Dry-run mutates nothing.
    assert all(i.state == "open" for i in issues.values())
    assert report["trackers_closed"] == [] and report["stories_closed"] == []


def test_reconcile_real_run_closes_completed_only(tmp_path: Path) -> None:
    issues = _seed_backfill_fixture(tmp_path)
    client = _Client(_Repo(issues))
    report = reconcile_completed_issues(
        _app_config(), client, software_factory_root=tmp_path, dry_run=False
    )
    # Completed work closed.
    assert issues[54].state == "closed"  # D005 tracker
    assert issues[88].state == "closed"  # D011 tracker (dual-draft!)
    assert issues[55].state == "closed"  # deployed winner
    assert issues[89].state == "closed"  # deployed winner
    assert issues[90].state == "closed"  # superseded loser
    # In-flight work UNTOUCHED.
    assert issues[294].state == "open"  # D099 tracker
    assert issues[295].state == "open"  # pr_open story
    assert not report["errors"]


def test_reconcile_is_idempotent(tmp_path: Path) -> None:
    issues = _seed_backfill_fixture(tmp_path)
    client = _Client(_Repo(issues))
    reconcile_completed_issues(_app_config(), client, software_factory_root=tmp_path)
    # Second pass closes nothing new and never re-edits an already-closed issue.
    report2 = reconcile_completed_issues(_app_config(), client, software_factory_root=tmp_path)
    assert report2["trackers_closed"] == [] and report2["stories_closed"] == []
    assert issues[54].edits.count({"state": "closed", "state_reason": None}) == 1


def test_reconcile_swallows_bad_issue_and_continues(tmp_path: Path) -> None:
    issues = _seed_backfill_fixture(tmp_path)

    class _BoomRepo(_Repo):
        def get_issue(self, n: int) -> _Issue:
            if n == 88:
                raise RuntimeError("gh 500")
            return super().get_issue(n)

    client = _Client(_BoomRepo(issues))
    report = reconcile_completed_issues(_app_config(), client, software_factory_root=tmp_path)
    # The one bad issue is recorded as an error but the sweep still closes the rest.
    assert any(num == 88 for _, num, _ in report["errors"])
    assert issues[54].state == "closed"
    assert issues[55].state == "closed"


def test_reconcile_bad_db_returns_error_not_raise(tmp_path: Path) -> None:
    # Adversarial-review MEDIUM: the DB read was outside try/except, so a
    # missing/locked/corrupt factory.db raised instead of returning an error
    # row — would break a tick if ever wired onto the tick path.
    client = _Client(_Repo({}))
    bogus = tmp_path / "factory.db"
    bogus.write_bytes(b"this is not a sqlite database")  # corrupt -> read raises
    report = reconcile_completed_issues(
        _app_config(), client, software_factory_root=tmp_path, db_path=bogus
    )
    assert report["errors"] and report["errors"][0][0] == "db"
    assert report["trackers_closed"] == [] and report["stories_closed"] == []


# ─── reconcile: fully-abandoned direction (no deploy) closes tracker + stories ──


def test_reconcile_closes_abandoned_direction_and_story_issues(tmp_path: Path) -> None:
    """An abandoned direction — both dual-draft siblings in terminal sinks
    (blocked_ci_unresolved + blocked_dependency_unmet), NOTHING deployed — must
    close its direction tracker AND both per-story issues. This is the 8-orphaned-
    sub-issue gap (2026-07-23): Pass 2 previously only closed DEPLOYED/SUPERSEDED
    story issues, leaving abandoned stories' issues open."""
    _make_direction(tmp_path, "093-email-verify", tracker_issue=268)
    _persist(
        tmp_path, direction_id="093", title="narrow", slug="ev-narrow-alt-a",
        scope="backend", state=StoryState.BLOCKED_CI_UNRESOLVED.value, github_issue_number=269,
    )
    _persist(
        tmp_path, direction_id="093", title="broad", slug="ev-broad-alt-b",
        scope="backend", state=StoryState.BLOCKED_DEPENDENCY_UNMET.value, github_issue_number=270,
    )
    issues = {268: _Issue(268), 269: _Issue(269), 270: _Issue(270)}
    client = _Client(_Repo(issues))
    report = reconcile_completed_issues(_app_config(), client, software_factory_root=tmp_path)
    assert (268, 268) not in report["trackers_closed"]  # tuple shape guard below
    # direction tracker closed
    assert 268 in {n for _, n in report["trackers_closed"]}
    # both abandoned story issues closed
    assert {n for _, n in report["stories_closed"]} == {269, 270}
    assert all(issues[n].state == "closed" for n in (268, 269, 270))


def test_reconcile_keeps_recoverable_block_story_issue_open(tmp_path: Path) -> None:
    """A story in a recoverable-pending-human block (NOT in _RESOLVED_STORY_STATES)
    keeps its issue open — the D094/D098 case (deploy_failed/budget/clarification)."""
    _make_direction(tmp_path, "094-pwreset", tracker_issue=271)
    _persist(
        tmp_path, direction_id="094", title="narrow", slug="pw-narrow-alt-a",
        scope="backend", state=StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value,
        github_issue_number=272,
    )
    issues = {271: _Issue(271), 272: _Issue(272)}
    client = _Client(_Repo(issues))
    report = reconcile_completed_issues(_app_config(), client, software_factory_root=tmp_path)
    assert report["trackers_closed"] == [] and report["stories_closed"] == []
    assert issues[271].state == "open" and issues[272].state == "open"


# ─── _should_run_issue_hygiene: hourly rate-gate ───────────────────────────────


def test_issue_hygiene_gate(tmp_path: Path) -> None:
    from factory.chain.orchestrator import (
        _issue_hygiene_marker,
        _mark_issue_hygiene_ran,
        _should_run_issue_hygiene,
    )

    # No marker yet → run.
    assert _should_run_issue_hygiene(tmp_path, "sacrifice") is True
    _mark_issue_hygiene_ran(tmp_path, "sacrifice")
    assert _issue_hygiene_marker(tmp_path, "sacrifice").exists()
    # Just ran → gated off.
    assert _should_run_issue_hygiene(tmp_path, "sacrifice") is False
    # Simulate >1h elapsed (now far in the future) → run again.
    import time as _t

    assert _should_run_issue_hygiene(tmp_path, "sacrifice", now=_t.time() + 7200) is True
    # Per-app: a different app with no marker still runs.
    assert _should_run_issue_hygiene(tmp_path, "factory") is True
