"""``_recover_blocked_stories`` re-dispatches blocked stories (loop-3).

A blocked story is a factory defect, never a real outcome. When the chain code
is fixed, stories already sitting in a terminal blocked state from the old
regime have no transition out — they'd stay blocked forever absent a manual DB
reset. This pass re-enters them into the TDD chain so the fix reaches them,
bounded per story so a genuinely unsatisfiable story escalates instead of
recycling forever.
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import SQLModel, create_engine

from factory.chain.event_log import log_story_event, read_story_events
from factory.chain.handlers import persist_story
from factory.chain.orchestrator import _MAX_AUTO_RECOVERIES, _recover_blocked_stories
from factory.chain.state_machine import StoryRecord, StoryState


def _seed(tmp_path: Path) -> Path:
    db = tmp_path / "state" / "factory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(create_engine(f"sqlite:///{db}", echo=False))
    return db


def _blocked_story(db: Path, *, state: str, slug: str, dev_retries: int = 6) -> StoryRecord:
    return persist_story(
        StoryRecord(
            direction_id="099", app="sacrifice", title="t", slug=slug,
            scope="backend", state=state, dev_retries=dev_retries,
            reviewer_cycles=6, error="dev exhausted retries (6)",
        ),
        db,
    )


def test_recovers_blocked_tests_need_clarification_to_sm_done(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    s = _blocked_story(db, state=StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value, slug="a")

    out = _recover_blocked_stories(db, "sacrifice", root=tmp_path)

    assert out == [("a", StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value, StoryState.SM_DONE.value)]
    # Reload and verify the clean slate.
    from sqlmodel import Session, select
    with Session(create_engine(f"sqlite:///{db}")) as ses:
        r = ses.exec(select(StoryRecord).where(StoryRecord.id == s.id)).one()
    assert r.state == StoryState.SM_DONE.value
    assert r.dev_retries == 0 and r.reviewer_cycles == 0 and r.error is None
    assert r.harness_precheck_passed is False
    events = read_story_events(s.id, software_factory_root=tmp_path, slug_hint=s.slug)
    assert [e for e in events if e.get("event") == "auto_recovery"]


def test_recovers_review_nonconvergent(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    _blocked_story(db, state=StoryState.BLOCKED_REVIEW_NONCONVERGENT.value, slug="b")
    out = _recover_blocked_stories(db, "sacrifice", root=tmp_path)
    assert out and out[0][2] == StoryState.SM_DONE.value


def test_deploy_failed_is_not_auto_recovered(tmp_path: Path) -> None:
    """deploy_failed is handled at the merge layer (auto_merge reconcile), not here."""
    db = _seed(tmp_path)
    _blocked_story(db, state=StoryState.BLOCKED_DEPLOY_FAILED.value, slug="c")
    assert _recover_blocked_stories(db, "sacrifice", root=tmp_path) == []


def test_recovery_is_bounded_then_escalates(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    s = _blocked_story(db, state=StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value, slug="d")

    # Simulate the story having already been recovered the max number of times.
    for i in range(_MAX_AUTO_RECOVERIES):
        log_story_event(
            s.id, "auto_recovery", {"attempt": i + 1},
            software_factory_root=tmp_path, slug_hint=s.slug,
        )

    out = _recover_blocked_stories(db, "sacrifice", root=tmp_path)
    assert out == []  # cap reached → not recovered again
    events = read_story_events(s.id, software_factory_root=tmp_path, slug_hint=s.slug)
    assert [e for e in events if e.get("event") == "auto_recovery_exhausted"]
