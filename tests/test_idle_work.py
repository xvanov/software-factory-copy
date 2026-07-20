"""Ceiling A: idle -> generate-work (keep the factory productively busy).

A well-maintained app drains; the factory then idles and its own detectors
manufacture stall-noise instead of generating new work. ``maybe_generate_idle_work``
closes that gap: when an app is genuinely drained it dispatches a work-generating
persona on demand, respecting each persona's daily cap and a multi-hour cooldown,
rotating across generators so the backlog refills with varied work.

Covered here:

  * a drained/idle app triggers a work-generating persona (rotation start);
  * a non-idle app does NOT (dispatch is never called);
  * the cooldown prevents thrash (second call within the window is a no-op);
  * rotation advances and skips a capped persona;
  * all-capped fires nothing (and doesn't burn the cooldown);
  * real dispatch honours the daily cap without ever calling the LLM
    (cap=0 -> rejected pre-dispatch -> all_capped);
  * a persona-filed direction is explore-tagged so it passes
    ``validate_direction`` instead of dead-ending at needs-direction;
  * a persona-filed direction flows to triage via ``auto_pm_sync`` with no
    operator step.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from sqlmodel import Session

from factory.chain.idle import (
    _IDLE_GEN_STATE_FILE,
    IdleWorkResult,
    maybe_generate_idle_work,
)
from factory.chain.scheduled_tasks import ScheduledRunRecord, _engine, run_scheduled_persona
from factory.chain.state_machine import StoryRecord, StoryState

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _write_root(tmp_path: Path, *, caps: dict[str, int] | None = None) -> Path:
    apps = tmp_path / "apps" / "sacrifice"
    apps.mkdir(parents=True)
    (apps / "config.yaml").write_text(
        yaml.safe_dump({"name": "sacrifice", "repo": "owner/sacrifice"}),
        encoding="utf-8",
    )
    (tmp_path / "state").mkdir()
    settings: dict = {
        "caps": {
            "global_concurrent_agents": 2,
            "per_repo_concurrent_agents": 2,
            "daily_spend_usd": 10.0,
            "hourly_spend_usd": 2.0,
        },
        "modes": {"default": "normal", "available": ["normal", "paused"]},
        "auto_pm_sync": {"enabled": True},
    }
    if caps is not None:
        settings["rate_limits"] = caps
    (tmp_path / "factory_settings.yaml").write_text(
        yaml.safe_dump(settings), encoding="utf-8"
    )
    from factory.settings.loader import reload_settings

    reload_settings(tmp_path)
    return tmp_path


class _FakeOut:
    """Minimal stand-in for ScheduledRunOutput (duck-typed)."""

    def __init__(
        self,
        *,
        status: str = "ok",
        findings_count: int = 1,
        directions_filed: list[str] | None = None,
    ) -> None:
        self.status = status
        self.findings_count = findings_count
        self.directions_filed = directions_filed if directions_filed is not None else ["001"]


class _Recorder:
    """Records dispatch calls and returns a scripted per-persona result."""

    def __init__(self, results: dict[str, _FakeOut]) -> None:
        self.results = results
        self.calls: list[str] = []

    def __call__(self, persona: str, app: str, root: Path, *, dry_run: bool = False) -> _FakeOut:
        self.calls.append(persona)
        return self.results.get(persona, _FakeOut())


def _seed_in_flight_story(root: Path) -> None:
    db = root / "state" / "factory.db"
    eng = _engine(db)
    with Session(eng) as session:
        session.add(
            StoryRecord(
                direction_id="001",
                app="sacrifice",
                title="In-flight",
                slug="in-flight",
                scope="backend",
                state=StoryState.DEV_IN_PROGRESS.value,
            )
        )
        session.commit()


def _read_marker(root: Path) -> dict:
    p = root / "state" / _IDLE_GEN_STATE_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# --------------------------------------------------------------------------- #
# Idle gate
# --------------------------------------------------------------------------- #


def test_drained_app_triggers_generator(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    rec = _Recorder({"bug_hunter": _FakeOut(findings_count=3, directions_filed=["001", "002"])})

    result = maybe_generate_idle_work("sacrifice", root, dispatch_fn=rec)

    assert isinstance(result, IdleWorkResult)
    assert result.fired is True
    # bug_hunter is first in the default rotation.
    assert result.persona == "bug_hunter"
    assert rec.calls == ["bug_hunter"]
    assert result.findings_count == 3
    assert result.directions_filed == 2
    # Cooldown + rotation marker persisted.
    marker = _read_marker(root)["sacrifice"]
    assert marker["next_index"] == 1
    assert marker["last_fired"]


def test_non_idle_app_does_not_generate(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    _seed_in_flight_story(root)
    rec = _Recorder({})

    result = maybe_generate_idle_work("sacrifice", root, dispatch_fn=rec)

    assert result.fired is False
    assert result.reason == "not_idle"
    assert rec.calls == []
    # No marker written when nothing fired.
    assert _read_marker(root) == {}


def test_recent_finding_keeps_app_non_idle(tmp_path: Path) -> None:
    """A cron persona that already filed findings this window means NOT idle,
    so the on-demand path stays quiet (no double-generation)."""
    root = _write_root(tmp_path)
    db = root / "state" / "factory.db"
    eng = _engine(db)
    with Session(eng) as session:
        session.add(
            ScheduledRunRecord(
                persona="bug_hunter",
                app="sacrifice",
                findings_count=1,
                directions_filed_json='["007"]',
                status="ok",
                dry_run=False,
                ts=datetime.now(UTC).isoformat(),
            )
        )
        session.commit()
    rec = _Recorder({})

    result = maybe_generate_idle_work("sacrifice", root, dispatch_fn=rec)
    assert result.fired is False
    assert result.reason == "not_idle"
    assert rec.calls == []


# --------------------------------------------------------------------------- #
# Cooldown / rotation / caps
# --------------------------------------------------------------------------- #


def test_cooldown_prevents_thrash(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    rec = _Recorder({})

    t0 = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    first = maybe_generate_idle_work("sacrifice", root, now=t0, dispatch_fn=rec)
    assert first.fired is True

    # A tick 5 minutes later must NOT re-dispatch.
    second = maybe_generate_idle_work(
        "sacrifice", root, now=t0 + timedelta(minutes=5), dispatch_fn=rec
    )
    assert second.fired is False
    assert second.reason == "cooldown"
    assert rec.calls == ["bug_hunter"]  # still only the first fire

    # Past the cooldown window it fires again, advancing the rotation.
    third = maybe_generate_idle_work(
        "sacrifice", root, now=t0 + timedelta(hours=7), dispatch_fn=rec
    )
    assert third.fired is True
    assert third.persona == "ux_auditor"  # rotation advanced
    assert rec.calls == ["bug_hunter", "ux_auditor"]


def test_rotation_skips_capped_persona(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    # bug_hunter is at its cap (rejected); ux_auditor has headroom.
    rec = _Recorder(
        {
            "bug_hunter": _FakeOut(status="rejected"),
            "ux_auditor": _FakeOut(findings_count=2),
        }
    )

    result = maybe_generate_idle_work("sacrifice", root, dispatch_fn=rec)

    assert result.fired is True
    assert result.persona == "ux_auditor"
    # Tried bug_hunter first, then fell through to ux_auditor.
    assert rec.calls == ["bug_hunter", "ux_auditor"]
    # next_index points to the slot AFTER ux_auditor (security -> index 2).
    assert _read_marker(root)["sacrifice"]["next_index"] == 2


def test_all_capped_fires_nothing_and_keeps_no_cooldown(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    rec = _Recorder(
        {
            "bug_hunter": _FakeOut(status="rejected"),
            "ux_auditor": _FakeOut(status="rejected"),
            "security": _FakeOut(status="rejected"),
        }
    )

    result = maybe_generate_idle_work("sacrifice", root, dispatch_fn=rec)

    assert result.fired is False
    assert result.reason == "all_capped"
    # Every generator was tried once.
    assert sorted(rec.calls) == ["bug_hunter", "security", "ux_auditor"]
    # No cooldown recorded, so a later tick (after a cap resets) can retry.
    assert _read_marker(root) == {}


def test_real_dispatch_honours_daily_cap_without_llm(tmp_path: Path) -> None:
    """Integration: cap=0 on every generator -> run_scheduled_persona rejects
    pre-dispatch (no LLM call) -> idle path reports all_capped."""
    root = _write_root(
        tmp_path,
        caps={
            "bug_hunter_runs_per_day": 0,
            "ux_auditor_runs_per_day": 0,
            "security_runs_per_day": 0,
        },
    )

    # No dispatch_fn -> uses the real run_scheduled_persona. cap=0 means the
    # rate-limit gate rejects before any LLM/fixture work.
    result = maybe_generate_idle_work("sacrifice", root, dry_run=False)

    assert result.fired is False
    assert result.reason == "all_capped"


def test_real_dry_run_dispatch_files_developable_direction(tmp_path: Path) -> None:
    """Integration: idle path -> real run_scheduled_persona(dry_run) fires the
    fixture, files a direction, and (dry_run) does NOT persist the cooldown."""
    root = _write_root(tmp_path)

    result = maybe_generate_idle_work("sacrifice", root, dry_run=True)

    assert result.fired is True
    assert result.persona == "bug_hunter"
    assert result.findings_count >= 1
    assert result.directions_filed >= 1
    # dry_run must not persist the marker (truly dry).
    assert _read_marker(root) == {}


# --------------------------------------------------------------------------- #
# Loop closure: finding -> direction -> (developable) -> triaged
# --------------------------------------------------------------------------- #


def test_persona_filed_direction_is_explore_and_developable(tmp_path: Path) -> None:
    """A scheduler persona files findings with no flow.md/api_spec — they must
    be explore-tagged so ``validate_direction`` accepts them instead of
    dead-ending at needs-direction."""
    from factory.backpressure.validator import validate_direction
    from factory.directions.parser import parse_direction_dir

    root = _write_root(tmp_path)
    out = run_scheduled_persona("bug_hunter", "sacrifice", root, dry_run=True)
    assert out.directions_filed

    # Dry-run writes to the scratch tree; parse the filed direction from there.
    scratch = root / "state" / "dry_run_scratch" / "apps" / "sacrifice" / "directions"
    ddirs = [d for d in scratch.iterdir() if d.is_dir()]
    assert ddirs
    direction = parse_direction_dir("sacrifice", ddirs[0])

    assert direction.explore_tag is True, "scheduler findings must be explore-tagged"
    vr = validate_direction(direction)
    assert vr.is_valid is True
    assert vr.explore_tag is True
    assert "explore_tag_or_artifacts" not in vr.missing


def test_finding_direction_flows_to_pm_sync_without_operator(tmp_path: Path) -> None:
    """A persona-filed (explore) direction is picked up by auto_pm_sync on the
    tick with no operator step."""
    from factory.chain.pm_sync import maybe_auto_pm_sync
    from factory.directions.creator import create_direction

    root = _write_root(tmp_path)
    # File a direction exactly as a scheduler persona does.
    create_direction(
        app="sacrifice",
        title="fix subprocess shell=True in exec.py",
        type_tag="security",
        why="Bug-hunter finding: shell injection risk.",
        has_ui=False,
        flow_steps=None,
        has_api=False,
        api_spec_lines=None,
        acceptance=["exec.py no longer uses shell=True on user input"],
        explore=True,
        attach_files=None,
        software_factory_root=root,
        source="scheduled-bug_hunter",
    )

    summary, reason = maybe_auto_pm_sync("sacrifice", root, dry_run=True)
    assert reason == "synced"
    assert summary is not None and summary.processed == 1
