"""Tests for ``close_abandoned_draft_sibling`` (audit 2026-07-18, leak 4 of 4).

The dual-draft flow spawns two ``draft-alternative`` StoryRecords per
ambiguous direction (``...-alt-a`` / ``...-alt-b``); whichever's PR merges
first should leave the other's issue (and branch) closed, but no code ever
did that — the tracker comment's promise ("the factory auto-cleans the
other draft once one alternative merges") was aspirational only. e.g. #210
stayed open forever after #209 (its sibling) merged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from factory.chain.dual_draft import close_abandoned_draft_sibling
from factory.chain.handlers import persist_story
from factory.chain.state_machine import StoryRecord, StoryState


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


class _AppConfig:
    name = "sacrifice"
    repo = "owner/sacrifice"


def _mk_pair(db: Path, *, winner_suffix: str = "alt-a", loser_suffix: str = "alt-b") -> tuple[StoryRecord, StoryRecord]:
    winner = persist_story(
        StoryRecord(
            direction_id="007",
            app="sacrifice",
            title="Make the thing better — narrow read",
            slug=f"make-it-better-{winner_suffix}",
            scope="backend",
            state=StoryState.DEPLOY_PENDING.value,
            github_issue_number=209,
            github_pr_number=555,
        ),
        db,
    )
    loser = persist_story(
        StoryRecord(
            direction_id="007",
            app="sacrifice",
            title="Make the thing better — broad read",
            slug=f"make-it-better-{loser_suffix}",
            scope="backend",
            state=StoryState.PR_OPEN.value,
            github_issue_number=210,
            github_pr_number=556,
        ),
        db,
    )
    return winner, loser


def test_close_abandoned_draft_sibling_closes_the_losing_issue(tmp_path: Path) -> None:
    db = tmp_path / "state" / "factory.db"
    winner, _loser = _mk_pair(db)

    sibling_issue = _Issue(210)
    client = _Client(_Repo({209: _Issue(209), 210: sibling_issue}))

    result = close_abandoned_draft_sibling(winner, _AppConfig(), tmp_path, db, client, False)

    assert result is True
    assert sibling_issue.state == "closed"
    assert sibling_issue.close_reason == "not_planned"
    assert sibling_issue.comments and "#209" in sibling_issue.comments[0]


def test_close_abandoned_draft_sibling_noop_when_already_closed(tmp_path: Path) -> None:
    db = tmp_path / "state" / "factory.db"
    winner, _loser = _mk_pair(db)

    sibling_issue = _Issue(210, state="closed")
    client = _Client(_Repo({209: _Issue(209), 210: sibling_issue}))

    result = close_abandoned_draft_sibling(winner, _AppConfig(), tmp_path, db, client, False)

    assert result is False
    assert sibling_issue.comments == []


def test_close_abandoned_draft_sibling_noop_when_dry_run(tmp_path: Path) -> None:
    db = tmp_path / "state" / "factory.db"
    winner, _loser = _mk_pair(db)

    sibling_issue = _Issue(210)
    client = _Client(_Repo({209: _Issue(209), 210: sibling_issue}))

    result = close_abandoned_draft_sibling(winner, _AppConfig(), tmp_path, db, client, True)

    assert result is False
    assert sibling_issue.state == "open"


def test_close_abandoned_draft_sibling_noop_when_no_github_client(tmp_path: Path) -> None:
    db = tmp_path / "state" / "factory.db"
    winner, _loser = _mk_pair(db)

    result = close_abandoned_draft_sibling(winner, _AppConfig(), tmp_path, db, None, False)

    assert result is False


def test_close_abandoned_draft_sibling_ignores_non_dual_draft_story(tmp_path: Path) -> None:
    """A normal (non-dual-draft) story's slug carries no ``-alt-*`` suffix —
    nothing to clean up, even if other stories share its direction_id."""
    db = tmp_path / "state" / "factory.db"
    winner = persist_story(
        StoryRecord(
            direction_id="008",
            app="sacrifice",
            title="Ordinary story",
            slug="ordinary-story",
            scope="backend",
            state=StoryState.DEPLOY_PENDING.value,
            github_issue_number=300,
            github_pr_number=301,
        ),
        db,
    )
    other = persist_story(
        StoryRecord(
            direction_id="008",
            app="sacrifice",
            title="Another ordinary story",
            slug="another-ordinary-story",
            scope="frontend",
            state=StoryState.PR_OPEN.value,
            github_issue_number=302,
            github_pr_number=303,
        ),
        db,
    )
    other_issue = _Issue(302)
    client = _Client(_Repo({300: _Issue(300), 302: other_issue}))

    result = close_abandoned_draft_sibling(winner, _AppConfig(), tmp_path, db, client, False)

    assert result is False
    assert other_issue.state == "open"


def test_close_abandoned_draft_sibling_ignores_same_interpretation(tmp_path: Path) -> None:
    """Two rows sharing the SAME alt suffix (shouldn't normally happen) must
    never be treated as "the other" sibling — never self-close."""
    db = tmp_path / "state" / "factory.db"
    winner = persist_story(
        StoryRecord(
            direction_id="009",
            app="sacrifice",
            title="dup a",
            slug="dup-thing-alt-a",
            scope="backend",
            state=StoryState.DEPLOY_PENDING.value,
            github_issue_number=400,
            github_pr_number=401,
        ),
        db,
    )
    same_suffix = persist_story(
        StoryRecord(
            direction_id="009",
            app="sacrifice",
            title="dup a again",
            slug="dup-thing-again-alt-a",
            scope="backend",
            state=StoryState.PR_OPEN.value,
            github_issue_number=402,
            github_pr_number=403,
        ),
        db,
    )
    issue = _Issue(402)
    client = _Client(_Repo({400: _Issue(400), 402: issue}))

    result = close_abandoned_draft_sibling(winner, _AppConfig(), tmp_path, db, client, False)

    assert result is False
    assert issue.state == "open"


def test_close_abandoned_draft_sibling_swallows_github_error(tmp_path: Path) -> None:
    db = tmp_path / "state" / "factory.db"
    winner, _loser = _mk_pair(db)

    class _BoomClient:
        def get_repo(self, full_name: str) -> Any:
            raise RuntimeError("gh down")

    # Must not raise — bookkeeping close is best-effort.
    result = close_abandoned_draft_sibling(winner, _AppConfig(), tmp_path, db, _BoomClient(), False)
    assert result is False
