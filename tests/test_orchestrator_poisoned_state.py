"""A story row with a state outside the StoryState enum must not abort the tick.

On 2026-07-07 a single row in state ``abandoned`` (not a StoryState value)
made ``_dispatch_for_story``'s ``StoryState(story.state)`` raise ValueError
and halt the entire factory for days. The tick must quarantine the poisoned
row (record it in ``summary.errors``, emit an event) and keep driving the
healthy stories.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

from factory.chain import handlers as H
from factory.chain import orchestrator as O
from factory.chain.state_machine import StoryRecord, StoryState


@pytest.fixture
def factory_root(tmp_path: Path) -> Path:
    apps_dir = tmp_path / "apps" / "sacrifice"
    apps_dir.mkdir(parents=True)
    (apps_dir / "config.yaml").write_text(
        "name: sacrifice\nrepo: ssh://placeholder\nrepo_path: /tmp/sacrifice\n",
        encoding="utf-8",
    )
    (tmp_path / "factory_settings.yaml").write_text(
        "caps:\n  global_concurrent_agents: 2\n  per_repo_concurrent_agents: 2\n"
        "  daily_spend_usd: 10\n  hourly_spend_usd: 2\n",
        encoding="utf-8",
    )
    return tmp_path


def test_poisoned_state_row_is_skipped_not_fatal(
    factory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = factory_root / "factory.db"
    eng = create_engine(f"sqlite:///{db}", echo=False)
    SQLModel.metadata.create_all(eng)
    # Distinct direction: the dependency-ordering gate would otherwise defer
    # the healthy story behind its lower-id (poisoned) sibling.
    poisoned = StoryRecord(
        direction_id="006",
        app="sacrifice",
        title="poisoned",
        slug="poisoned",
        scope="backend",
        state="abandoned",  # not a StoryState value
        chain_kind="tdd",
    )
    healthy = StoryRecord(
        direction_id="007",
        app="sacrifice",
        title="healthy",
        slug="healthy",
        scope="backend",
        state=StoryState.STORY_CREATED.value,
        chain_kind="tdd",
    )
    with Session(eng) as session:
        session.add(poisoned)
        session.add(healthy)
        session.commit()
        session.refresh(poisoned)
        session.refresh(healthy)
        poisoned_id, healthy_id = poisoned.id, healthy.id

    dispatched: list[int | None] = []

    def _fake_sm(story: StoryRecord, *_a: object, **_k: object) -> H.HandlerResult:
        dispatched.append(story.id)
        story.state = StoryState.SM_IN_PROGRESS.value
        H.persist_story(story, db_path=db)
        return H.HandlerResult(next_state=StoryState.SM_IN_PROGRESS)

    monkeypatch.setattr(H, "handle_sm", _fake_sm)

    # Must not raise despite the poisoned row.
    summary = O.tick(factory_root, "sacrifice", db_path=db, max_advances_per_story=1)

    # Poisoned row surfaced as a non-fatal error, healthy story still driven.
    assert any("invalid state" in msg for _, msg in summary.errors)
    assert dispatched == [healthy_id]

    # Poisoned row untouched (quarantined, not mutated or deleted).
    with Session(eng) as session:
        refreshed = session.get(StoryRecord, poisoned_id)
        assert refreshed is not None
        assert refreshed.state == "abandoned"
