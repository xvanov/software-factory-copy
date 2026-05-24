"""Tests for Phase 7 dual-draft PR flow.

Verifies the trigger conditions in ``should_spawn_dual_draft`` and the
end-to-end spawn-of-two behavior through ``handle_stories_spawned``:

  * ``(explore)``-tagged direction → 2 stories spawned
  * PM confidence < 0.6 → 2 stories spawned
  * confidence ≥ 0.6 + no explore → 1 story spawned (normal path)

The chain spawn tests run in dry-run mode (no GH client) so we exercise
the StoryRecord-creation path without any network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlmodel import Session, create_engine, select

from factory.app_config import AppConfig
from factory.chain.dual_draft import (
    CONFIDENCE_THRESHOLD,
    Interpretation,
    produce_interpretations,
    should_spawn_dual_draft,
)
from factory.chain.handlers import handle_stories_spawned
from factory.chain.state_machine import StoryRecord
from factory.directions.parser import Direction


def _mk_direction(*, explore: bool = False, title: str = "Make the thing better") -> Direction:
    return Direction(
        id="001",
        slug="make-it-better",
        title=title,
        type_tag=None,
        why="Because the metric is wrong.",
        has_flow=False,
        has_api_spec=False,
        acceptance=["Outcome is observably correct."],
        explore_tag=explore,
        artifacts_paths=[],
        app="sacrifice",
        status="pm-validated",
        raw_frontmatter={"title": title, "explore": explore},
        raw_body=f"# {title}",
        dir_path=Path("."),
        state={"tracker_issue": 42},
    )


def test_should_spawn_dual_draft_explore_tag_only() -> None:
    """An ``(explore)`` direction always fires dual-draft, even at high confidence."""
    d = _mk_direction(explore=True)
    assert should_spawn_dual_draft(d, {"confidence": 0.95}) is True


def test_should_spawn_dual_draft_low_confidence_only() -> None:
    """Non-explore direction below the confidence threshold fires dual-draft."""
    d = _mk_direction(explore=False)
    assert should_spawn_dual_draft(d, {"confidence": 0.5}) is True
    # Exactly at threshold = NOT ambiguous.
    assert should_spawn_dual_draft(d, {"confidence": CONFIDENCE_THRESHOLD}) is False


def test_should_spawn_dual_draft_high_confidence_no_explore() -> None:
    """High-confidence, non-explore → normal single-story path."""
    d = _mk_direction(explore=False)
    assert should_spawn_dual_draft(d, {"confidence": 0.85}) is False


def test_should_spawn_dual_draft_handles_missing_confidence() -> None:
    """Missing confidence + no explore → not ambiguous (assume PM was definite)."""
    d = _mk_direction(explore=False)
    assert should_spawn_dual_draft(d, {}) is False


def test_produce_interpretations_dry_run_returns_two() -> None:
    d = _mk_direction(explore=True)
    interps = produce_interpretations(d, {}, dry_run=True)
    assert len(interps) == 2
    assert all(isinstance(i, Interpretation) for i in interps)
    ids = sorted(i.interpretation_id for i in interps)
    assert ids == ["alt-a", "alt-b"]
    # Each interpretation must carry a non-trivial key_assumption_diff.
    for interp in interps:
        assert len(interp.key_assumption_diff) > 10
        assert d.title in interp.title


def _setup_dry_run_root(tmp_path: Path) -> tuple[Path, AppConfig]:
    """Sacrifice app config + state dir, no GH."""
    apps = tmp_path / "apps" / "sacrifice"
    apps.mkdir(parents=True)
    cfg_text = yaml.safe_dump({"name": "sacrifice", "repo": "owner/sacrifice"})
    (apps / "config.yaml").write_text(cfg_text, encoding="utf-8")
    (tmp_path / "state").mkdir()
    cfg = AppConfig(name="sacrifice", repo="owner/sacrifice")
    return tmp_path, cfg


def _stories_for(app: str, db: Path) -> list[StoryRecord]:
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        return list(session.exec(select(StoryRecord).where(StoryRecord.app == app)).all())


def test_handle_stories_spawned_explore_spawns_two_stories(tmp_path: Path) -> None:
    root, cfg = _setup_dry_run_root(tmp_path)
    direction = _mk_direction(explore=True)
    pm_result = {
        "child_stories": [{"title": "Touch the thing", "scope": "backend"}],
        "confidence": 0.9,  # high — but explore overrides
    }
    stories = handle_stories_spawned(
        direction=direction,
        pm_result=pm_result,
        app_config=cfg,
        software_factory_root=root,
        dry_run=True,
    )
    assert len(stories) == 2, "expected 2 stories from dual-draft branch"
    slugs = sorted(s.slug for s in stories)
    assert any("alt-a" in s for s in slugs), f"alt-a missing in slugs={slugs}"
    assert any("alt-b" in s for s in slugs), f"alt-b missing in slugs={slugs}"

    # DB persistence: both rows present.
    persisted = _stories_for("sacrifice", root / "state" / "factory.db")
    assert len(persisted) == 2


def test_handle_stories_spawned_low_confidence_spawns_two_stories(tmp_path: Path) -> None:
    root, cfg = _setup_dry_run_root(tmp_path)
    direction = _mk_direction(explore=False)
    pm_result = {
        "child_stories": [{"title": "Touch the thing", "scope": "frontend"}],
        "confidence": 0.5,  # below 0.6 threshold
    }
    stories = handle_stories_spawned(
        direction=direction,
        pm_result=pm_result,
        app_config=cfg,
        software_factory_root=root,
        dry_run=True,
    )
    assert len(stories) == 2
    # scope of the first child must be carried into both spawn rows.
    assert all(s.scope == "frontend" for s in stories)


def test_handle_stories_spawned_high_confidence_spawns_one_story(tmp_path: Path) -> None:
    root, cfg = _setup_dry_run_root(tmp_path)
    direction = _mk_direction(explore=False)
    pm_result = {
        "child_stories": [{"title": "Touch the thing", "scope": "backend"}],
        "confidence": 0.85,
    }
    stories = handle_stories_spawned(
        direction=direction,
        pm_result=pm_result,
        app_config=cfg,
        software_factory_root=root,
        dry_run=True,
    )
    assert len(stories) == 1, "expected single-story path"
    assert "alt-a" not in stories[0].slug
    assert "alt-b" not in stories[0].slug


_ = Any  # silence unused import
