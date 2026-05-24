"""Tests for the Phase-6 cron scheduler.

Covers:

  * ``due_schedules`` returns first-run entries when no rows exist.
  * After a run is recorded with last_run = now, ``due_schedules`` does
    NOT re-fire until the next cron boundary.
  * Rate-limit cap (``ralph_runs_per_day``) flips entries to
    ``rate_limit_hit=True`` once the cap is hit.
  * ``load_schedules`` falls back to defaults when factory_settings.yaml
    lacks a ``schedules:`` block.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from factory.chain.scheduled_tasks import ScheduledRunRecord
from factory.scheduler.cron import (
    Schedule,
    due_schedules,
    load_schedules,
    next_fire,
    upsert_schedule_row,
)
from factory.settings.loader import reload_settings


def _write_root(tmp_path: Path, with_schedules: bool = True) -> Path:
    """Set up a tmp factory root with optional schedules block."""
    apps = tmp_path / "apps" / "sacrifice"
    apps.mkdir(parents=True)
    (apps / "config.yaml").write_text("name: sacrifice\nrepo: o/r\n", encoding="utf-8")
    settings: dict[str, object] = {
        "rate_limits": {
            "ralph_runs_per_day": 24,
        },
        "modes": {"default": "normal", "available": ["normal"]},
    }
    if with_schedules:
        settings["schedules"] = [
            {
                "name": "ralph",
                "cron": "0 * * * *",
                "persona": "ralph",
                "rate_limit_key": "ralph_runs_per_day",
            },
            {"name": "bug_hunt", "cron": "0 6 * * *", "persona": "bug_hunter"},
        ]
    (tmp_path / "factory_settings.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    (tmp_path / "state").mkdir()
    reload_settings(tmp_path)
    return tmp_path


def test_first_tick_returns_all_schedules(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    due = due_schedules(root, now=datetime(2026, 6, 1, 10, 5, tzinfo=UTC))
    names = sorted(d.schedule.name for d in due)
    assert names == ["bug_hunt", "ralph"]
    assert all(d.reason == "first_run" for d in due)
    assert all(not d.rate_limit_hit for d in due)


def test_run_recorded_blocks_re_firing(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    db = root / "state" / "factory.db"
    # Record a run at 10:00. The previous cron fire for "0 * * * *" at
    # 10:05 is also 10:00 → already covered.
    upsert_schedule_row(
        name="ralph",
        cron_expr="0 * * * *",
        last_run="2026-06-01T10:00:00+00:00",
        last_status="ok",
        db_path=db,
    )
    due = due_schedules(root, now=datetime(2026, 6, 1, 10, 5, tzinfo=UTC), db_path=db)
    names = [d.schedule.name for d in due]
    # bug_hunt fires at 06:00; never run → still due.
    assert "ralph" not in names
    assert "bug_hunt" in names


def test_rate_limit_flags_due_schedule(tmp_path: Path) -> None:
    """When the cap is reached, the schedule still surfaces but rate_limit_hit=True."""
    from sqlmodel import Session

    from factory.scheduler.cron import _engine

    root = _write_root(tmp_path)
    db = root / "state" / "factory.db"
    # Insert 24 successful scheduled runs of ``ralph`` in the last 24h.
    eng = _engine(db)
    now = datetime(2026, 6, 1, 10, 5, tzinfo=UTC)
    with Session(eng) as session:
        for i in range(24):
            session.add(
                ScheduledRunRecord(
                    ts=(now - timedelta(hours=i)).isoformat(),
                    persona="ralph",
                    app="sacrifice",
                    status="ok",
                )
            )
        session.commit()
    due = due_schedules(root, now=now, db_path=db)
    ralph = [d for d in due if d.schedule.name == "ralph"]
    assert len(ralph) == 1
    assert ralph[0].rate_limit_hit is True


def test_fallback_defaults_when_no_schedules_block(tmp_path: Path) -> None:
    """Missing ``schedules:`` block in YAML → defaults still load."""
    root = _write_root(tmp_path, with_schedules=False)
    schedules = load_schedules(root)
    names = sorted(s.name for s in schedules)
    assert names == ["bug_hunt", "ralph", "security_weekly", "ux_audit"]


def test_next_fire_returns_future_time() -> None:
    s = Schedule(name="t", cron_expr="0 * * * *", persona="ralph")
    now = datetime(2026, 6, 1, 10, 5, tzinfo=UTC)
    nxt = next_fire(s, now=now)
    assert nxt == datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
