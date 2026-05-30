"""Tests for the ``stalled_stories`` liveness detector.

This detector closes the monitoring blind spot where a silently-stuck factory
emitted no events, the windowed detectors saw an empty window, and the watcher
reported "quiet, healthy." These tests assert it fires on ABSOLUTE state aging
regardless of event activity.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from factory.manager.detectors import DETECTORS
from factory.manager.detectors.stalled_stories import stalled_stories


def _make_db(root: Path, rows: list[tuple[int, str, str]]) -> None:
    """Seed a minimal stories table. rows = (id, state, updated_at_iso)."""
    (root / "state").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / "state" / "factory.db"))
    conn.execute(
        "CREATE TABLE stories (id INTEGER PRIMARY KEY, state TEXT, app TEXT, "
        "slug TEXT, updated_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO stories (id, state, app, slug, updated_at) VALUES (?,?,?,?,?)",
        [(i, st, "sacrifice", f"s{i}", ts) for (i, st, ts) in rows],
    )
    conn.commit()
    conn.close()


def _write_tick(root: Path, ts: datetime) -> None:
    stream = root / "state" / "events" / "ticks.ndjson"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text(json.dumps({"ts": ts.isoformat(), "event": "tick"}) + "\n")


def test_is_registered() -> None:
    assert "stalled_stories" in DETECTORS


def test_empty_when_everything_fresh(tmp_path: Path) -> None:
    now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    fresh = (now - timedelta(minutes=2)).isoformat()
    _make_db(tmp_path, [(1, "dev_in_progress", fresh), (2, "sm_done", fresh)])
    _write_tick(tmp_path, now - timedelta(minutes=1))

    res = stalled_stories(root=tmp_path, now=now)
    assert res["alarms"] == []
    assert res["stuck_in_progress"] == []
    assert res["stalled"] == []
    assert res["no_tick_recently"] is False


def test_stuck_in_progress_fires(tmp_path: Path) -> None:
    now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    old = (now - timedelta(minutes=45)).isoformat()
    _make_db(tmp_path, [(7, "dev_in_progress", old)])
    _write_tick(tmp_path, now - timedelta(minutes=1))

    res = stalled_stories(root=tmp_path, now=now)
    assert len(res["stuck_in_progress"]) == 1
    assert res["stuck_in_progress"][0]["story_id"] == 7
    assert res["alarms"], "a stuck-in-progress story must raise an alarm"


def test_stalled_nonterminal_fires(tmp_path: Path) -> None:
    now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    old = (now - timedelta(minutes=200)).isoformat()
    _make_db(tmp_path, [(9, "sm_done", old)])
    _write_tick(tmp_path, now - timedelta(minutes=1))

    res = stalled_stories(root=tmp_path, now=now)
    assert len(res["stalled"]) == 1
    assert res["stalled"][0]["story_id"] == 9
    assert res["alarms"]


def test_terminal_states_never_alarm(tmp_path: Path) -> None:
    now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    ancient = (now - timedelta(days=3)).isoformat()
    _make_db(
        tmp_path,
        [
            (1, "deployed", ancient),
            (2, "blocked_review_nonconvergent", ancient),
            (3, "blocked_tests_need_clarification", ancient),
        ],
    )
    _write_tick(tmp_path, now - timedelta(minutes=1))

    res = stalled_stories(root=tmp_path, now=now)
    assert res["alarms"] == []
    assert res["non_terminal_total"] == 0


def test_no_tick_recently_fires_even_with_no_stories(tmp_path: Path) -> None:
    """The core blind-spot case: nothing in flight, but the drive loop is dead.
    The old windowed watcher reported 'quiet'; this must report an alarm."""
    now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    _make_db(tmp_path, [])
    _write_tick(tmp_path, now - timedelta(minutes=42))

    res = stalled_stories(root=tmp_path, now=now)
    assert res["no_tick_recently"] is True
    assert res["minutes_since_last_tick"] == 42.0
    assert any("drive loop" in a for a in res["alarms"])


def test_does_not_depend_on_event_window(tmp_path: Path) -> None:
    """Unlike the windowed detectors, stalled_stories takes no ``since`` and
    fires purely on absolute aging — proving it sees a silent stall."""
    now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    old = (now - timedelta(minutes=300)).isoformat()
    _make_db(tmp_path, [(1, "tests_green", old), (2, "reviewer_in_progress", old)])
    # No tick stream at all → minutes_since_last_tick is None, not an alarm.
    res = stalled_stories(root=tmp_path, now=now)
    assert res["minutes_since_last_tick"] is None
    assert res["no_tick_recently"] is False
    # But the two aged non-terminal stories still alarm.
    assert len(res["stalled"]) + len(res["stuck_in_progress"]) == 2
    assert res["alarms"]
