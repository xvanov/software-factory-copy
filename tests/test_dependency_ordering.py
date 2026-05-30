"""Tests for direction-internal dependency ordering (_direction_deps_pending)."""
from __future__ import annotations

from pathlib import Path

from sqlmodel import SQLModel, create_engine

from factory.chain.handlers import persist_story
from factory.chain.orchestrator import _direction_deps_pending
from factory.chain.state_machine import StoryRecord, StoryState


def _seed(tmp_path: Path) -> Path:
    db = tmp_path / "state" / "factory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(create_engine(f"sqlite:///{db}"))
    return db


def _story(db: Path, *, sid: int, direction: str, state: str, app: str = "sacrifice") -> StoryRecord:
    return persist_story(
        StoryRecord(id=sid, direction_id=direction, app=app, title="t",
                    slug=f"s{sid}", scope="backend", state=state),
        db,
    )


def test_foundational_story_has_no_pending_deps(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    s14 = _story(db, sid=14, direction="008", state=StoryState.SM_DONE.value)
    assert _direction_deps_pending(db, s14) == []


def test_dependent_waits_for_undeployed_lower_siblings(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    _story(db, sid=14, direction="008", state=StoryState.TESTS_RED.value)   # not deployed
    _story(db, sid=16, direction="008", state=StoryState.DEPLOYED.value)    # deployed
    s18 = _story(db, sid=18, direction="008", state=StoryState.SM_DONE.value)
    # 14 (not deployed) blocks; 16 (deployed) does not.
    assert _direction_deps_pending(db, s18) == [14]


def test_ready_when_all_lower_siblings_deployed(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    _story(db, sid=14, direction="008", state=StoryState.DEPLOYED.value)
    _story(db, sid=16, direction="008", state=StoryState.DEPLOYED.value)
    s18 = _story(db, sid=18, direction="008", state=StoryState.SM_DONE.value)
    assert _direction_deps_pending(db, s18) == []


def test_cross_direction_does_not_block(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    _story(db, sid=5, direction="007", state=StoryState.TESTS_RED.value)  # other direction
    s14 = _story(db, sid=14, direction="008", state=StoryState.SM_DONE.value)
    assert _direction_deps_pending(db, s14) == []
