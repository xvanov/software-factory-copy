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
