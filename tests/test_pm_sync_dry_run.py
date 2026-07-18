"""End-to-end pm_sync test in dry-run mode.

Exercises the real ``pm_sync`` function (no monkeypatching the entry point).
The only thing mocked is the LLM call (skipped via ``dry_run=True``) and the
GitHub client (None; pm_sync does not call it in dry-run).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from factory.chain.pm_sync import pm_sync
from factory.directions.creator import create_direction


def _seed_app_config(tmp_path: Path) -> None:
    apps_dir = tmp_path / "apps" / "sacrifice"
    apps_dir.mkdir(parents=True)
    (apps_dir / "config.yaml").write_text(
        "name: sacrifice\nrepo: xvanov/sacrifice\ndefault_branch: main\n"
        "context_dir: context\ndeploy:\n  enabled: false\nmodels: {}\n",
        encoding="utf-8",
    )


def test_pm_sync_dry_run_two_complete_one_vague(tmp_path: Path) -> None:
    _seed_app_config(tmp_path)

    # 001 — complete with API spec.
    create_direction(
        app="sacrifice",
        title="Add healthz endpoint",
        type_tag="feature",
        why="Smoke test wants a stable endpoint.",
        has_ui=False,
        flow_steps=None,
        has_api=True,
        api_spec_lines=['- `POST /healthz` -> 200 {"status":"ok"}'],
        acceptance=["Returns 200", "JSON body has status"],
        explore=False,
        attach_files=None,
        software_factory_root=tmp_path,
    )

    # 002 — complete with a UI flow.
    create_direction(
        app="sacrifice",
        title="Celebration screen",
        type_tag="feature",
        why="Users want a moment of joy after pledging.",
        has_ui=True,
        flow_steps=[
            "User completes pledge",
            "App displays celebration screen with confetti",
            "User dismisses; returns to dashboard",
        ],
        has_api=False,
        api_spec_lines=None,
        acceptance=["Confetti renders", "Screen dismisses on click"],
        explore=False,
        attach_files=None,
        software_factory_root=tmp_path,
    )

    # 003 — vague. No flow, no api_spec, no explore tag.
    create_direction(
        app="sacrifice",
        title="Vague thought",
        type_tag=None,
        why="I have a feeling.",
        has_ui=False,
        flow_steps=None,
        has_api=False,
        api_spec_lines=None,
        acceptance=[],
        explore=False,
        attach_files=None,
        software_factory_root=tmp_path,
    )

    state_db = tmp_path / "state" / "factory.db"
    summary = pm_sync(
        app="sacrifice",
        software_factory_root=tmp_path,
        dry_run=True,
        state_db_path=state_db,
    )

    assert summary.processed == 3
    assert summary.validated == 2
    assert summary.needs_direction == 1
    assert summary.errors == []

    # Verify each direction's state.yaml was actually updated.
    directions_dir = tmp_path / "apps" / "sacrifice" / "directions"
    by_status: dict[str, list[str]] = {}
    for entry in sorted(directions_dir.iterdir()):
        if not entry.is_dir():
            continue
        state = yaml.safe_load((entry / "state.yaml").read_text(encoding="utf-8"))
        by_status.setdefault(state["status"], []).append(entry.name)
        # Every direction must have a pm_result blob now.
        assert "pm_result" in state
        assert isinstance(state["pm_result"], dict)
        # Audit trail recorded the status transition.
        assert any(
            entry.get("event", "").startswith("status -> ") for entry in state.get("audit", [])
        )

    assert "pm-validated" in by_status
    assert "needs-direction" in by_status
    assert len(by_status["pm-validated"]) == 2
    assert len(by_status["needs-direction"]) == 1
    # The vague one is the needs-direction one.
    assert by_status["needs-direction"][0].endswith("-vague-thought")


def test_pm_sync_gc_pass_closes_stale_scheduled_direction(tmp_path: Path) -> None:
    """pm_sync's end-of-pass GC (factory.directions.gc) closes a scheduler-filed
    direction that's been stuck at needs-direction well past the threshold —
    the fix for audit 2026-07-18 leak 2 of 4 (directions filed by scheduler
    personas that never got operator follow-up rotted at needs-direction
    forever)."""
    from datetime import UTC, datetime, timedelta

    from factory.directions.gc import GC_BY, MAX_AGE_DAYS

    _seed_app_config(tmp_path)
    created = create_direction(
        app="sacrifice",
        title="rate-limit pledge endpoint",
        type_tag="security",
        why="pledge flooding",
        has_ui=False,
        flow_steps=None,
        has_api=False,
        api_spec_lines=None,
        acceptance=["429 after 5/min"],
        explore=True,
        attach_files=None,
        software_factory_root=tmp_path,
        source="scheduled-security",
    )
    state_path = created.dir_path / "state.yaml"
    state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    old = (datetime.now(UTC) - timedelta(days=MAX_AGE_DAYS + 1)).isoformat()
    state["created_at"] = old
    state["status"] = "needs-direction"
    state["audit"] = [{"event": "status -> needs-direction"}]
    state_path.write_text(yaml.safe_dump(state, sort_keys=False), encoding="utf-8")

    summary = pm_sync(
        app="sacrifice",
        software_factory_root=tmp_path,
        dry_run=True,
        state_db_path=tmp_path / "state" / "factory.db",
        # Narrow to "created" so this stale needs-direction entry is not
        # re-validated by the normal pm loop — only the GC pass should
        # touch it, mirroring the automated (maybe_auto_pm_sync) caller.
        pending_statuses=frozenset({"created"}),
    )

    assert summary.gc_closed == [created.direction.id]
    final_state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    assert final_state["status"] == "closed"
    assert final_state["audit"][-1]["by"] == GC_BY


def test_pm_sync_gc_pass_leaves_fresh_directions_alone(tmp_path: Path) -> None:
    """A freshly-filed scheduled direction (or one from the main test fixture)
    must not be touched by the GC pass."""
    _seed_app_config(tmp_path)
    create_direction(
        app="sacrifice",
        title="fresh scheduled finding",
        type_tag="security",
        why="just filed",
        has_ui=False,
        flow_steps=None,
        has_api=False,
        api_spec_lines=None,
        acceptance=["fixed"],
        explore=True,
        attach_files=None,
        software_factory_root=tmp_path,
        source="scheduled-bug_hunter",
    )
    summary = pm_sync(
        app="sacrifice",
        software_factory_root=tmp_path,
        dry_run=True,
        state_db_path=tmp_path / "state" / "factory.db",
    )
    assert summary.gc_closed == []


def test_pm_sync_dry_run_no_directions_empty_summary(tmp_path: Path) -> None:
    _seed_app_config(tmp_path)
    # Need at least the directions directory to exist.
    (tmp_path / "apps" / "sacrifice" / "directions").mkdir(exist_ok=True)
    summary = pm_sync(
        app="sacrifice",
        software_factory_root=tmp_path,
        dry_run=True,
        state_db_path=tmp_path / "state" / "factory.db",
    )
    assert summary.processed == 0
    assert summary.validated == 0
    assert summary.needs_direction == 0
