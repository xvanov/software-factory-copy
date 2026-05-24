"""open_or_update_tracker_issue idempotency + needs-direction comment."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from factory.app_config import AppConfig, DeployConfig
from factory.directions.creator import create_direction
from factory.directions.parser import parse_direction_dir
from factory.directions.tracker_issue import (
    open_or_update_tracker_issue,
    record_needs_direction,
)


class _FakeLabel:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeIssue:
    def __init__(self, number: int, title: str, body: str, labels: list[str]) -> None:
        self.number = number
        self.title = title
        self.body = body
        self.labels = [_FakeLabel(lbl) for lbl in labels]
        self.comments: list[str] = []
        self.edit_calls: list[dict[str, Any]] = []

    def edit(
        self,
        *,
        title: str | None = None,
        body: str | None = None,
        labels: list[str] | None = None,
    ) -> None:
        if title is not None:
            self.title = title
        if body is not None:
            self.body = body
        if labels is not None:
            self.labels = [_FakeLabel(lbl) for lbl in labels]
        self.edit_calls.append(
            {"title": self.title, "body": self.body, "labels": [lb.name for lb in self.labels]}
        )

    def create_comment(self, body: str) -> None:
        self.comments.append(body)


class _FakeRepo:
    def __init__(self) -> None:
        self.issues: dict[int, _FakeIssue] = {}
        self.create_calls: list[dict[str, Any]] = []
        self._next = 100

    def create_issue(self, *, title: str, body: str, labels: list[str]) -> _FakeIssue:
        n = self._next
        self._next += 1
        issue = _FakeIssue(n, title, body, labels)
        self.issues[n] = issue
        self.create_calls.append({"title": title, "body": body, "labels": labels})
        return issue

    def get_issue(self, n: int) -> _FakeIssue:
        return self.issues[n]


class _FakeGithub:
    def __init__(self) -> None:
        self.repo = _FakeRepo()
        self.get_repo_calls: list[str] = []

    def get_repo(self, full_name: str) -> _FakeRepo:
        self.get_repo_calls.append(full_name)
        return self.repo


def _seed(tmp_path: Path):  # type: ignore[no-untyped-def]
    out = create_direction(
        app="sacrifice",
        title="Add healthz",
        type_tag="feature",
        why="Smoke test.",
        has_ui=False,
        flow_steps=None,
        has_api=True,
        api_spec_lines=["- POST /healthz -> 200"],
        acceptance=["AC"],
        explore=False,
        attach_files=None,
        software_factory_root=tmp_path,
    )
    return parse_direction_dir("sacrifice", out.dir_path)


def _app_config() -> AppConfig:
    return AppConfig(
        name="sacrifice",
        repo="xvanov/sacrifice",
        default_branch="main",
        context_dir="context",
        deploy=DeployConfig(enabled=False),
        models={},
    )


def test_creates_issue_on_first_call(tmp_path: Path) -> None:
    direction = _seed(tmp_path)
    gh = _FakeGithub()
    cfg = _app_config()

    pm_result = {
        "type": "feature",
        "priority": "p2",
        "has_sufficient_backpressure": True,
        "missing": [],
        "tracker_title": "Add healthz endpoint",
        "tracker_body": "We need /healthz for smoke tests.",
        "child_stories": [{"title": "stub", "scope": "backend", "rationale": "x"}],
        "labels": ["feature", "priority/p2"],
        "confidence": 0.8,
    }
    number = open_or_update_tracker_issue(direction, cfg, gh, pm_result=pm_result)
    assert number == 100
    assert len(gh.repo.create_calls) == 1
    call = gh.repo.create_calls[0]
    assert call["title"].startswith("[DIRECTION]")
    assert "feature" in call["labels"]
    assert "direction-tracker" in call["labels"]
    assert "priority/p2" in call["labels"]
    # The body must include the PM's summary text.
    assert "smoke tests" in call["body"]
    # state.yaml has the issue number persisted.
    re_parsed = parse_direction_dir("sacrifice", direction.dir_path)
    assert re_parsed.state["tracker_issue"] == 100


def test_idempotent_no_duplicate_issue(tmp_path: Path) -> None:
    direction = _seed(tmp_path)
    gh = _FakeGithub()
    cfg = _app_config()
    pm_result = {
        "type": "feature",
        "priority": "p2",
        "has_sufficient_backpressure": True,
        "missing": [],
        "tracker_title": "Add healthz endpoint",
        "tracker_body": "We need /healthz.",
        "child_stories": [],
        "labels": ["feature", "priority/p2"],
        "confidence": 0.8,
    }
    n1 = open_or_update_tracker_issue(direction, cfg, gh, pm_result=pm_result)
    # Re-parse so the fresh state.yaml is loaded into the direction
    direction = parse_direction_dir("sacrifice", direction.dir_path)

    n2 = open_or_update_tracker_issue(direction, cfg, gh, pm_result=pm_result)
    assert n1 == n2 == 100
    assert len(gh.repo.create_calls) == 1
    # The existing issue's edit was called on the second call.
    assert len(gh.repo.issues[100].edit_calls) == 1


def test_record_needs_direction_labels_and_comments(tmp_path: Path) -> None:
    direction = _seed(tmp_path)
    gh = _FakeGithub()
    cfg = _app_config()
    pm_result = {
        "type": "feature",
        "priority": "p2",
        "has_sufficient_backpressure": False,
        "missing": ["user_flow", "acceptance_criteria"],
        "tracker_title": "Vague request",
        "tracker_body": "_(needs more info)_",
        "child_stories": [],
        "labels": ["feature", "priority/p2"],
        "confidence": 0.4,
    }
    number = record_needs_direction(
        direction,
        ["user_flow", "acceptance_criteria"],
        cfg,
        gh,
        pm_result=pm_result,
    )
    assert number == 100
    issue = gh.repo.issues[number]
    assert "needs-direction" in [lbl.name for lbl in issue.labels]
    assert any("user_flow" in c for c in issue.comments)
