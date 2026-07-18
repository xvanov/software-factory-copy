"""Deployed stories close their GitHub issues + direction tracker.

Regression test for the 2026-07-18 audit finding: the chain set stories to
DEPLOYED but never closed their issues, so 64 issues for shipped work sat open.
"""

from __future__ import annotations

from typing import Any

from factory.directions.tracker_issue import close_story_issue


class _Issue:
    def __init__(self, number: int, state: str = "open") -> None:
        self.number = number
        self.state = state
        self.comments: list[str] = []
        self.edits: list[str] = []

    def create_comment(self, body: str) -> None:
        self.comments.append(body)

    def edit(self, state: str) -> None:
        self.edits.append(state)
        self.state = state


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


class _Story:
    def __init__(self, issue_number: int | None) -> None:
        self.github_issue_number = issue_number


def test_deployed_story_issue_is_closed() -> None:
    issue = _Issue(42)
    client = _Client(_Repo({42: issue}))
    assert close_story_issue(_Story(42), _AppConfig(), client) is True
    assert issue.state == "closed"
    assert issue.comments and "Deployed" in issue.comments[0]


def test_already_closed_issue_is_noop() -> None:
    issue = _Issue(42, state="closed")
    client = _Client(_Repo({42: issue}))
    assert close_story_issue(_Story(42), _AppConfig(), client) is False
    assert issue.edits == []


def test_story_without_issue_number_is_noop() -> None:
    client = _Client(_Repo({}))
    assert close_story_issue(_Story(None), _AppConfig(), client) is False


def test_github_error_is_swallowed() -> None:
    class _BoomClient:
        def get_repo(self, full_name: str) -> Any:
            raise RuntimeError("gh down")

    # Must not raise — bookkeeping close is best-effort.
    assert close_story_issue(_Story(42), _AppConfig(), _BoomClient()) is False
