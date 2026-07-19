"""CI-failure -> dev re-fix loop (``auto_merge._handle_ci_failure``).

Closes the gap the operator called out: real CI (``_query_ci_state``)
already gates merges on ``"failure"``, but a failing PR just sat there —
nothing fed the failure back to dev. ``_handle_ci_failure`` re-dispatches the
story to dev with the CI failure surfaced through the EXISTING
reviewer-findings plumbing, bounded by a hard cap plus a failure-signature
guard (mirroring ``orchestrator._recover_blocked_stories``) so a CI failure
the dev cannot fix escalates instead of looping forever.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from factory.app_config import AppConfig
from factory.chain import auto_merge as am
from factory.chain.event_log import log_story_event, read_story_events
from factory.chain.handlers import persist_story
from factory.chain.state_machine import StoryRecord, StoryState


def _seed(tmp_path: Path) -> Path:
    db = tmp_path / "state" / "factory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(create_engine(f"sqlite:///{db}", echo=False))
    return db


def _pr_open_story(db: Path, *, slug: str = "s") -> StoryRecord:
    return persist_story(
        StoryRecord(
            direction_id="042",
            app="sacrifice",
            title="t",
            slug=slug,
            scope="backend",
            state=StoryState.PR_OPEN.value,
            github_pr_number=77,
        ),
        db,
    )


def _cfg() -> AppConfig:
    return AppConfig(name="sacrifice", repo="o/sacrifice", default_branch="main")


# --------------------------------------------------------------------------- #
# _fetch_ci_failure_logs — best-effort gh parsing, mocked subprocess
# --------------------------------------------------------------------------- #


def test_fetch_ci_failure_logs_returns_digest_via_details_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    calls: list[list[str]] = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "view"]:
            payload = {
                "headRefName": "story-77-fix",
                "statusCheckRollup": [
                    {
                        "conclusion": "SUCCESS",
                        "detailsUrl": "https://github.com/o/sacrifice/actions/runs/111/job/1",
                    },
                    {
                        "conclusion": "FAILURE",
                        "detailsUrl": "https://github.com/o/sacrifice/actions/runs/222/job/2",
                    },
                ],
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        if cmd[:3] == ["gh", "run", "view"]:
            assert cmd[3] == "222"
            return subprocess.CompletedProcess(
                cmd, 0, "FAIL tests/test_x.py::test_y\nAssertionError: boom", ""
            )
        raise AssertionError(f"unexpected gh invocation: {cmd}")

    monkeypatch.setattr(subprocess, "run", _fake_run, raising=True)
    digest = am._fetch_ci_failure_logs(app_config=_cfg(), pr_number=77)
    assert "AssertionError: boom" in digest
    # Picked the FAILURE run's id (222), not the SUCCESS one (111) or a
    # ``gh run list`` fallback.
    assert not any(c[:3] == ["gh", "run", "list"] for c in calls)


def test_fetch_ci_failure_logs_falls_back_to_run_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def _fake_run(cmd, **kw):
        if cmd[:3] == ["gh", "pr", "view"]:
            payload = {"headRefName": "story-77-fix", "statusCheckRollup": []}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        if cmd[:3] == ["gh", "run", "list"]:
            runs = [{"databaseId": 333, "conclusion": "failure", "status": "completed"}]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(runs), "")
        if cmd[:3] == ["gh", "run", "view"]:
            assert cmd[3] == "333"
            return subprocess.CompletedProcess(cmd, 0, "job failed: exit 1", "")
        raise AssertionError(f"unexpected gh invocation: {cmd}")

    monkeypatch.setattr(subprocess, "run", _fake_run, raising=True)
    digest = am._fetch_ci_failure_logs(app_config=_cfg(), pr_number=77)
    assert "job failed" in digest


def test_fetch_ci_failure_logs_trims_to_4000_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    long_log = "x" * 10_000

    def _fake_run(cmd, **kw):
        if cmd[:3] == ["gh", "pr", "view"]:
            payload = {
                "headRefName": "b",
                "statusCheckRollup": [
                    {"conclusion": "FAILURE", "detailsUrl": "https://x/actions/runs/9/job/1"}
                ],
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        if cmd[:3] == ["gh", "run", "view"]:
            return subprocess.CompletedProcess(cmd, 0, long_log, "")
        raise AssertionError(cmd)

    monkeypatch.setattr(subprocess, "run", _fake_run, raising=True)
    digest = am._fetch_ci_failure_logs(app_config=_cfg(), pr_number=1)
    assert len(digest) == 4000
    assert digest == long_log[-4000:]


def test_fetch_ci_failure_logs_returns_empty_on_placeholder_pr() -> None:
    assert am._fetch_ci_failure_logs(app_config=_cfg(), pr_number=0) == ""
    assert am._fetch_ci_failure_logs(app_config=_cfg(), pr_number=-5) == ""


def test_fetch_ci_failure_logs_returns_empty_on_gh_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def _raise(cmd, **kw):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr(subprocess, "run", _raise, raising=True)
    assert am._fetch_ci_failure_logs(app_config=_cfg(), pr_number=7) == ""


def test_fetch_ci_failure_logs_returns_empty_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def _raise(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(subprocess, "run", _raise, raising=True)
    assert am._fetch_ci_failure_logs(app_config=_cfg(), pr_number=7) == ""


def test_fetch_ci_failure_logs_returns_empty_on_no_failed_run_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def _fake_run(cmd, **kw):
        if cmd[:3] == ["gh", "pr", "view"]:
            payload = {"headRefName": "", "statusCheckRollup": []}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        raise AssertionError(cmd)

    monkeypatch.setattr(subprocess, "run", _fake_run, raising=True)
    assert am._fetch_ci_failure_logs(app_config=_cfg(), pr_number=7) == ""


# --------------------------------------------------------------------------- #
# _handle_ci_failure — bounded re-dispatch
# --------------------------------------------------------------------------- #


def test_first_ci_failure_redispatches_to_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _seed(tmp_path)
    story = _pr_open_story(db)
    monkeypatch.setattr(
        am, "_fetch_ci_failure_logs", lambda **kw: "FAIL test_x.py: AssertionError boom"
    )

    redispatched = am._handle_ci_failure(
        story=story, app_config=_cfg(), pr_number=77, db=db, root=tmp_path
    )
    assert redispatched is True

    with Session(create_engine(f"sqlite:///{db}")) as ses:
        r = ses.exec(select(StoryRecord).where(StoryRecord.id == story.id)).one()
    assert r.state == StoryState.REVIEWER_REQUESTED_CHANGES.value
    assert r.dev_retries == 0
    assert r.reviewer_result_json is not None
    payload = json.loads(r.reviewer_result_json)
    assert payload["source"] == "ci_failure"
    assert payload["findings"]
    # The CI-failure finding is a well-formed dict (not a bare string): a string
    # element crashed every consumer's f.get(...) and silently broke this loop.
    finding = payload["findings"][0]
    assert isinstance(finding, dict)
    assert "AssertionError boom" in finding["what"]

    events = read_story_events(story.id, software_factory_root=tmp_path, slug_hint=story.slug)
    redispatch_events = [e for e in events if e.get("event") == "ci_fix_redispatch"]
    assert len(redispatch_events) == 1
    assert redispatch_events[0]["pr_number"] == 77
    assert redispatch_events[0]["failure_signature"]


def test_identical_failure_signature_does_not_redispatch_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _seed(tmp_path)
    story = _pr_open_story(db)
    monkeypatch.setattr(
        am, "_fetch_ci_failure_logs", lambda **kw: "FAIL test_x.py: AssertionError boom"
    )

    first = am._handle_ci_failure(
        story=story, app_config=_cfg(), pr_number=77, db=db, root=tmp_path
    )
    assert first is True

    # Story comes back around to PR_OPEN (real CI re-ran) with the SAME
    # failure — the dev's fix attempt didn't actually fix it.
    story.state = StoryState.PR_OPEN.value
    persist_story(story, db)

    second = am._handle_ci_failure(
        story=story, app_config=_cfg(), pr_number=77, db=db, root=tmp_path
    )
    assert second is False

    with Session(create_engine(f"sqlite:///{db}")) as ses:
        r = ses.exec(select(StoryRecord).where(StoryRecord.id == story.id)).one()
    assert r.state == StoryState.PR_OPEN.value  # untouched — not re-dispatched

    events = read_story_events(story.id, software_factory_root=tmp_path, slug_hint=story.slug)
    assert len([e for e in events if e.get("event") == "ci_fix_redispatch"]) == 1
    exhausted = [e for e in events if e.get("event") == "ci_fix_exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0]["reason"] == "identical_failure_signature"


def test_different_failure_signature_redispatches_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _seed(tmp_path)
    story = _pr_open_story(db)
    logs = {"text": "FAIL test_x.py: AssertionError boom"}
    monkeypatch.setattr(am, "_fetch_ci_failure_logs", lambda **kw: logs["text"])

    first = am._handle_ci_failure(
        story=story, app_config=_cfg(), pr_number=77, db=db, root=tmp_path
    )
    assert first is True

    story.state = StoryState.PR_OPEN.value
    persist_story(story, db)
    logs["text"] = "FAIL test_y.py: TypeError unexpected kwarg"

    second = am._handle_ci_failure(
        story=story, app_config=_cfg(), pr_number=77, db=db, root=tmp_path
    )
    assert second is True

    events = read_story_events(story.id, software_factory_root=tmp_path, slug_hint=story.slug)
    redispatch_events = [e for e in events if e.get("event") == "ci_fix_redispatch"]
    assert len(redispatch_events) == 2
    assert (
        redispatch_events[0]["failure_signature"] != redispatch_events[1]["failure_signature"]
    )
    assert not [e for e in events if e.get("event") == "ci_fix_exhausted"]

    with Session(create_engine(f"sqlite:///{db}")) as ses:
        r = ses.exec(select(StoryRecord).where(StoryRecord.id == story.id)).one()
    assert r.state == StoryState.REVIEWER_REQUESTED_CHANGES.value


def test_cap_reached_does_not_redispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _seed(tmp_path)
    story = _pr_open_story(db)
    monkeypatch.setattr(am, "_fetch_ci_failure_logs", lambda **kw: "irrelevant")

    # Simulate _MAX_CI_FIX_CYCLES prior redispatches already logged, each with
    # a DIFFERENT signature so the signature guard itself never trips first —
    # this isolates the cap check.
    for i in range(am._MAX_CI_FIX_CYCLES):
        log_story_event(
            story.id,
            "ci_fix_redispatch",
            {"pr_number": 77, "attempt": i + 1, "failure_signature": f"sig-{i}"},
            software_factory_root=tmp_path,
            slug_hint=story.slug,
        )

    redispatched = am._handle_ci_failure(
        story=story, app_config=_cfg(), pr_number=77, db=db, root=tmp_path
    )
    assert redispatched is False

    with Session(create_engine(f"sqlite:///{db}")) as ses:
        r = ses.exec(select(StoryRecord).where(StoryRecord.id == story.id)).one()
    assert r.state == StoryState.PR_OPEN.value  # untouched

    events = read_story_events(story.id, software_factory_root=tmp_path, slug_hint=story.slug)
    exhausted = [e for e in events if e.get("event") == "ci_fix_exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0]["reason"] == "cap_reached"
    # No NEW redispatch was recorded beyond the simulated prior ones.
    assert len([e for e in events if e.get("event") == "ci_fix_redispatch"]) == am._MAX_CI_FIX_CYCLES


def test_ci_fix_exhausted_is_deduped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling _handle_ci_failure repeatedly past the cap emits exactly one
    ci_fix_exhausted event, not one per call (mirrors auto_recovery_exhausted
    dedup in orchestrator._recover_blocked_stories)."""
    db = _seed(tmp_path)
    story = _pr_open_story(db)
    monkeypatch.setattr(am, "_fetch_ci_failure_logs", lambda **kw: "irrelevant")
    for i in range(am._MAX_CI_FIX_CYCLES):
        log_story_event(
            story.id,
            "ci_fix_redispatch",
            {"pr_number": 77, "attempt": i + 1, "failure_signature": f"sig-{i}"},
            software_factory_root=tmp_path,
            slug_hint=story.slug,
        )

    am._handle_ci_failure(story=story, app_config=_cfg(), pr_number=77, db=db, root=tmp_path)
    am._handle_ci_failure(story=story, app_config=_cfg(), pr_number=77, db=db, root=tmp_path)

    events = read_story_events(story.id, software_factory_root=tmp_path, slug_hint=story.slug)
    assert len([e for e in events if e.get("event") == "ci_fix_exhausted"]) == 1


def test_does_not_redispatch_story_not_in_mergeable_state(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    story = persist_story(
        StoryRecord(
            direction_id="042", app="sacrifice", title="t", slug="dev",
            scope="backend", state=StoryState.DEV_IN_PROGRESS.value,
            github_pr_number=77,
        ),
        db,
    )
    redispatched = am._handle_ci_failure(
        story=story, app_config=_cfg(), pr_number=77, db=db, root=tmp_path
    )
    assert redispatched is False
    with Session(create_engine(f"sqlite:///{db}")) as ses:
        r = ses.exec(select(StoryRecord).where(StoryRecord.id == story.id)).one()
    assert r.state == StoryState.DEV_IN_PROGRESS.value  # untouched


# --------------------------------------------------------------------------- #
# Wiring — auto_merge_tick calls _handle_ci_failure before the merge decision
# --------------------------------------------------------------------------- #


def test_auto_merge_tick_redispatches_on_real_ci_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps_dir = tmp_path / "apps" / "sacrifice"
    apps_dir.mkdir(parents=True)
    (apps_dir / "config.yaml").write_text("name: sacrifice\nrepo: o/sacrifice\n", encoding="utf-8")
    db = _seed(tmp_path)
    story = _pr_open_story(db)

    monkeypatch.setattr(am, "_fetch_ci_failure_logs", lambda **kw: "FAIL: boom")

    fixture = am.FixturePR(
        pr_number=77,
        head_sha="deadbeef",
        base_branch="main",
        labels=[],
        files_changed=["src/x.py"],
        ci_state="failure",
        story=story,
    )
    actions = am.auto_merge_tick(
        tmp_path, "sacrifice", dry_run=False, fixture_prs=[fixture], db_path=db,
    )
    assert len(actions) == 1
    assert actions[0].merged is False
    assert "re-dispatched" in actions[0].reason

    with Session(create_engine(f"sqlite:///{db}")) as ses:
        r = ses.exec(select(StoryRecord).where(StoryRecord.id == story.id)).one()
    assert r.state == StoryState.REVIEWER_REQUESTED_CHANGES.value


def test_auto_merge_tick_dry_run_unaffected_by_ci_failure(tmp_path: Path) -> None:
    """dry-run fixtures with ci_state='failure' must not be re-dispatched —
    the CI-failure loop only fires in real-run."""
    apps_dir = tmp_path / "apps" / "sacrifice"
    apps_dir.mkdir(parents=True)
    (apps_dir / "config.yaml").write_text("name: sacrifice\nrepo: o/sacrifice\n", encoding="utf-8")
    db = _seed(tmp_path)
    story = _pr_open_story(db)

    fixture = am.FixturePR(
        pr_number=77,
        head_sha="deadbeef",
        base_branch="main",
        labels=[],
        files_changed=["src/x.py"],
        ci_state="failure",
        story=story,
    )
    actions = am.auto_merge_tick(
        tmp_path, "sacrifice", dry_run=True, fixture_prs=[fixture], db_path=db,
    )
    assert len(actions) == 1
    assert actions[0].merged is False
    assert "re-dispatched" not in actions[0].reason

    with Session(create_engine(f"sqlite:///{db}")) as ses:
        r = ses.exec(select(StoryRecord).where(StoryRecord.id == story.id)).one()
    assert r.state == StoryState.PR_OPEN.value  # untouched in dry-run


def test_auto_merge_tick_placeholder_pr_unaffected_by_ci_failure(tmp_path: Path) -> None:
    """A negative (placeholder) pr_number must never be re-dispatched even if
    ci_state somehow reads 'failure' — no real PR exists to investigate."""
    apps_dir = tmp_path / "apps" / "sacrifice"
    apps_dir.mkdir(parents=True)
    (apps_dir / "config.yaml").write_text("name: sacrifice\nrepo: o/sacrifice\n", encoding="utf-8")
    db = _seed(tmp_path)
    story = persist_story(
        StoryRecord(
            direction_id="042", app="sacrifice", title="t", slug="ph",
            scope="backend", state=StoryState.PR_OPEN.value,
        ),
        db,
    )
    fixture = am.FixturePR(
        pr_number=-(story.id or 0),
        head_sha="deadbeef",
        base_branch="main",
        labels=[],
        files_changed=["src/x.py"],
        ci_state="failure",
        story=story,
    )
    actions = am.auto_merge_tick(
        tmp_path, "sacrifice", dry_run=False, fixture_prs=[fixture], db_path=db,
    )
    assert len(actions) == 1
    assert "re-dispatched" not in actions[0].reason
    with Session(create_engine(f"sqlite:///{db}")) as ses:
        r = ses.exec(select(StoryRecord).where(StoryRecord.id == story.id)).one()
    assert r.state == StoryState.PR_OPEN.value
