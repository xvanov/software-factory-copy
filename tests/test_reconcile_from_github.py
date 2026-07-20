"""``reconcile_from_github`` pulls authoritative GitHub PR state into the DB.

Local ``factory.db`` state is a PROJECTION; GitHub is the system of record for
whether a PR merged, closed, or is still open. The projection drifts (a merge
completed out-of-band while the local story still says ``pr_open``; a PR closed
while the story keeps looping on a dead branch). This pass runs at the TOP of a
tick and reconciles each non-terminal story that has a real PR against GitHub
BEFORE any dispatch decision, logging every reconciliation as a first-class
``state_drift_reconciled`` anomaly.

The ``gh`` shell-out is injected via ``query_pr_state`` so these tests never
touch the network.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from factory.app_config import AppConfig
from factory.chain.event_log import read_story_events
from factory.chain.handlers import persist_story
from factory.chain.orchestrator import reconcile_from_github
from factory.chain.state_machine import StoryRecord, StoryState

_CFG = AppConfig(name="sacrifice", repo="acme/sacrifice")


def _seed(tmp_path: Path) -> Path:
    db = tmp_path / "state" / "factory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(create_engine(f"sqlite:///{db}", echo=False))
    return db


def _story(
    db: Path,
    *,
    state: str,
    slug: str,
    pr_number: int | None = 42,
) -> StoryRecord:
    return persist_story(
        StoryRecord(
            direction_id="099", app="sacrifice", title="t", slug=slug,
            scope="backend", state=state, github_pr_number=pr_number,
            github_branch=f"factory/{slug}",
        ),
        db,
    )


def _reload(db: Path, story_id: int | None) -> StoryRecord:
    with Session(create_engine(f"sqlite:///{db}")) as ses:
        return ses.exec(select(StoryRecord).where(StoryRecord.id == story_id)).one()


def _fixed_state(value: str | None):
    """A ``query_pr_state`` stub that always returns ``value``."""
    def _q(*, app_config: AppConfig, pr_number: int) -> str | None:
        return value
    return _q


def _git_events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "state" / "events" / "git.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _drift_events(tmp_path: Path) -> list[dict]:
    return [e for e in _git_events(tmp_path) if e.get("event") == "state_drift_reconciled"]


# --------------------------------------------------------------------------- #
# Drift case: PR MERGED on GitHub but local state pre-merge
# --------------------------------------------------------------------------- #


def test_merged_on_github_advances_local_to_deploy_pending(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    s = _story(db, state=StoryState.PR_OPEN.value, slug="merged")

    out = reconcile_from_github(
        db, "sacrifice", cfg=_CFG, root=tmp_path,
        query_pr_state=_fixed_state("MERGED"),
    )

    assert out == [("merged", StoryState.PR_OPEN.value, StoryState.DEPLOY_PENDING.value)]
    assert _reload(db, s.id).state == StoryState.DEPLOY_PENDING.value

    # First-class drift anomaly on the git stream (an L1 watcher _RAW_STREAM).
    drift = _drift_events(tmp_path)
    assert len(drift) == 1
    ev = drift[0]
    assert ev["story_id"] == s.id
    assert ev["local_state_before"] == StoryState.PR_OPEN.value
    assert ev["authoritative_pr_state"] == "MERGED"
    assert ev["action"] == f"advanced_to:{StoryState.DEPLOY_PENDING.value}"
    assert ev["pr_number"] == 42

    # And on the per-story timeline.
    story_events = read_story_events(s.id, software_factory_root=tmp_path, slug_hint=s.slug)
    assert [e for e in story_events if e.get("event") == "state_drift_reconciled"]


def test_merged_advances_from_ci_green_and_ready_for_merge(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    a = _story(db, state=StoryState.CI_GREEN.value, slug="cg", pr_number=7)
    b = _story(db, state=StoryState.READY_FOR_MERGE.value, slug="rfm", pr_number=8)

    out = reconcile_from_github(
        db, "sacrifice", cfg=_CFG, root=tmp_path,
        query_pr_state=_fixed_state("MERGED"),
    )

    to_states = {slug: to for slug, _, to in out}
    assert to_states == {
        "cg": StoryState.DEPLOY_PENDING.value,
        "rfm": StoryState.DEPLOY_PENDING.value,
    }
    assert _reload(db, a.id).state == StoryState.DEPLOY_PENDING.value
    assert _reload(db, b.id).state == StoryState.DEPLOY_PENDING.value


# --------------------------------------------------------------------------- #
# Drift case: PR CLOSED (not merged) on GitHub
# --------------------------------------------------------------------------- #


def test_closed_on_github_moves_story_to_attention_state(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    s = _story(db, state=StoryState.PR_OPEN.value, slug="closed")

    out = reconcile_from_github(
        db, "sacrifice", cfg=_CFG, root=tmp_path,
        query_pr_state=_fixed_state("CLOSED"),
    )

    assert out == [
        ("closed", StoryState.PR_OPEN.value, StoryState.BLOCKED_DEPLOY_FAILED.value)
    ]
    r = _reload(db, s.id)
    assert r.state == StoryState.BLOCKED_DEPLOY_FAILED.value
    assert r.error and "CLOSED on GitHub" in r.error

    drift = _drift_events(tmp_path)
    assert len(drift) == 1
    assert drift[0]["authoritative_pr_state"] == "CLOSED"
    assert drift[0]["action"] == f"advanced_to:{StoryState.BLOCKED_DEPLOY_FAILED.value}"


# --------------------------------------------------------------------------- #
# No-op cases
# --------------------------------------------------------------------------- #


def test_open_pr_is_noop_no_event(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    s = _story(db, state=StoryState.PR_OPEN.value, slug="open")

    out = reconcile_from_github(
        db, "sacrifice", cfg=_CFG, root=tmp_path,
        query_pr_state=_fixed_state("OPEN"),
    )

    assert out == []
    assert _reload(db, s.id).state == StoryState.PR_OPEN.value
    assert _drift_events(tmp_path) == []


def test_gh_query_failure_is_failsafe_noop(tmp_path: Path) -> None:
    """A gh query returning None (gh missing / timeout / unresolvable) must NOT
    advance the story — never reconcile on uncertainty — and must not crash."""
    db = _seed(tmp_path)
    s = _story(db, state=StoryState.PR_OPEN.value, slug="unknown")

    out = reconcile_from_github(
        db, "sacrifice", cfg=_CFG, root=tmp_path,
        query_pr_state=_fixed_state(None),
    )

    assert out == []
    assert _reload(db, s.id).state == StoryState.PR_OPEN.value  # untouched
    assert _drift_events(tmp_path) == []


def test_terminal_and_no_pr_stories_are_skipped(tmp_path: Path) -> None:
    """Only non-terminal stories in a mergeable state WITH a real PR are
    candidates. A deployed (terminal) story and a mergeable story lacking a PR
    number must never be queried or advanced."""
    db = _seed(tmp_path)
    terminal = _story(db, state=StoryState.DEPLOYED.value, slug="done", pr_number=99)
    no_pr = _story(db, state=StoryState.PR_OPEN.value, slug="nopr", pr_number=None)
    placeholder = _story(db, state=StoryState.PR_OPEN.value, slug="ph", pr_number=0)

    calls: list[int] = []

    def _tracking_q(*, app_config: AppConfig, pr_number: int) -> str | None:
        calls.append(pr_number)
        return "MERGED"

    out = reconcile_from_github(
        db, "sacrifice", cfg=_CFG, root=tmp_path, query_pr_state=_tracking_q,
    )

    assert out == []
    assert calls == []  # none of the three were queried
    assert _reload(db, terminal.id).state == StoryState.DEPLOYED.value
    assert _reload(db, no_pr.id).state == StoryState.PR_OPEN.value
    assert _reload(db, placeholder.id).state == StoryState.PR_OPEN.value


# --------------------------------------------------------------------------- #
# Idempotency + bounding
# --------------------------------------------------------------------------- #


def test_reconcile_is_idempotent(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    s = _story(db, state=StoryState.PR_OPEN.value, slug="idem")

    first = reconcile_from_github(
        db, "sacrifice", cfg=_CFG, root=tmp_path,
        query_pr_state=_fixed_state("MERGED"),
    )
    assert first  # advanced once

    # Re-run: the story is now in DEPLOY_PENDING (not a mergeable candidate),
    # so nothing is queried and no new event is emitted.
    second = reconcile_from_github(
        db, "sacrifice", cfg=_CFG, root=tmp_path,
        query_pr_state=_fixed_state("MERGED"),
    )
    assert second == []
    assert len(_drift_events(tmp_path)) == 1  # still only the first event
    assert _reload(db, s.id).state == StoryState.DEPLOY_PENDING.value


def test_reconcile_is_bounded_per_tick(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    for i in range(5):
        _story(db, state=StoryState.PR_OPEN.value, slug=f"s{i}", pr_number=100 + i)

    calls: list[int] = []

    def _tracking_q(*, app_config: AppConfig, pr_number: int) -> str | None:
        calls.append(pr_number)
        return "OPEN"  # no-op action, we only care about the call count

    out = reconcile_from_github(
        db, "sacrifice", cfg=_CFG, root=tmp_path,
        query_pr_state=_tracking_q, max_reconcile=2,
    )

    assert out == []
    assert len(calls) == 2  # capped — the other 3 wait for a later tick
