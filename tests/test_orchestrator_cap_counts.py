"""Concurrency-cap counters exclude queued ``STORY_CREATED`` stories.

Without this exclusion, a PM-sync batch that spawns N children at once
(all entering ``STORY_CREATED`` simultaneously) deadlocks the chain: each
story sees the other N-1 as "in-flight competitors" and the enforcer
refuses every dispatch with ``global_concurrent_agents_cap_exceeded``.
``STORY_CREATED`` is a pre-dispatch queue state — no agent is running on
those rows yet — so they must not count toward the cap.
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from factory.chain.orchestrator import _count_app_in_flight, _count_global_in_flight
from factory.chain.state_machine import StoryRecord, StoryState


def _seed_db(tmp_path: Path, rows: list[StoryRecord]) -> Path:
    db = tmp_path / "factory.db"
    eng = create_engine(f"sqlite:///{db}", echo=False)
    SQLModel.metadata.create_all(eng)
    with Session(eng) as session:
        for r in rows:
            session.add(r)
        session.commit()
    return db


def _story(state: StoryState, app: str = "sacrifice", slug: str = "s") -> StoryRecord:
    return StoryRecord(
        direction_id="007",
        app=app,
        title="t",
        slug=slug,
        scope="backend",
        state=state.value,
    )


def test_story_created_does_not_count_toward_global_cap(tmp_path: Path) -> None:
    # 13 stories all queued in STORY_CREATED — the PM-sync batch shape.
    rows = [_story(StoryState.STORY_CREATED, slug=f"s{i}") for i in range(13)]
    db = _seed_db(tmp_path, rows)
    assert _count_global_in_flight(db) == 0


def test_dispatched_stories_do_count_toward_global_cap(tmp_path: Path) -> None:
    rows = [
        _story(StoryState.STORY_CREATED, slug="queued"),
        _story(StoryState.SM_IN_PROGRESS, slug="running"),
        _story(StoryState.DEV_IN_PROGRESS, slug="running2"),
    ]
    db = _seed_db(tmp_path, rows)
    assert _count_global_in_flight(db) == 2


def test_terminal_stories_do_not_count(tmp_path: Path) -> None:
    rows = [
        _story(StoryState.PR_OPEN, slug="pr"),
        _story(StoryState.DEPLOYED, slug="dep"),
        _story(StoryState.BLOCKED_DEPLOY_FAILED, slug="bad"),
    ]
    db = _seed_db(tmp_path, rows)
    assert _count_global_in_flight(db) == 0


def test_exclude_story_id_removes_self(tmp_path: Path) -> None:
    rows = [
        _story(StoryState.SM_IN_PROGRESS, slug="a"),
        _story(StoryState.DEV_IN_PROGRESS, slug="b"),
    ]
    db = _seed_db(tmp_path, rows)
    # Re-read to grab IDs assigned at commit time.
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        ids = [r.id for r in session.exec(__import__("sqlmodel").select(StoryRecord)).all()]
    assert _count_global_in_flight(db) == 2
    assert _count_global_in_flight(db, exclude_story_id=ids[0]) == 1


def test_app_in_flight_scopes_to_app(tmp_path: Path) -> None:
    rows = [
        _story(StoryState.SM_IN_PROGRESS, app="sacrifice", slug="s1"),
        _story(StoryState.DEV_IN_PROGRESS, app="sacrifice", slug="s2"),
        _story(StoryState.SM_IN_PROGRESS, app="other", slug="o1"),
    ]
    db = _seed_db(tmp_path, rows)
    assert _count_app_in_flight(db, "sacrifice") == 2
    assert _count_app_in_flight(db, "other") == 1


def test_app_in_flight_excludes_story_created(tmp_path: Path) -> None:
    rows = [_story(StoryState.STORY_CREATED, slug=f"s{i}") for i in range(5)]
    db = _seed_db(tmp_path, rows)
    assert _count_app_in_flight(db, "sacrifice") == 0
