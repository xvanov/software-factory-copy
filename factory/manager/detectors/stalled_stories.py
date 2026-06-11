"""Detector: stalled_stories — surface ABSOLUTE liveness, not event deltas.

This detector exists to close the monitoring blind spot that let the factory
sit silently stuck for hours: every other detector reads a short rolling
*event window* (``since`` = ~last minute), so when the chain stalls it stops
emitting events, the window goes empty, and the watcher reports "quiet,
healthy." Silence was read as health.

``stalled_stories`` ignores the window entirely. It reads the CURRENT story
state straight from ``state/factory.db`` and the timestamp of the last tick
from ``state/events/ticks.ndjson``, and measures ages relative to ``now``. A
stuck factory produces no events but its DB rows keep aging — so this fires
exactly when the others go blind.

It surfaces three independent liveness signals; the calling L1 agent should
treat a non-empty ``alarms`` list as escalate-worthy:

  * ``stuck_in_progress`` — a story sat in a ``*_in_progress`` state longer
    than ``in_progress_stall_minutes``. A handler was dispatched and never
    returned (process killed mid-run, hang, dirty-tree race, uncaught
    exception). The chain's own stale-recovery may eventually reclaim it, but
    a growing count here means dispatch is wedged.
  * ``stalled`` — a non-terminal, non-deployed story hasn't changed state in
    ``stall_minutes``. Forward progress has stopped for that story.
  * ``no_tick_recently`` — the orchestrator hasn't ticked in
    ``tick_silence_minutes``. The drive loop is likely dead; nothing is being
    dispatched at all.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# States the chain never advances out of — aging in these is expected and fine.
_TERMINAL_STATES = frozenset(
    {
        "deployed",
        "blocked_tests_need_clarification",
        "blocked_deploy_failed",
        "blocked_review_nonconvergent",
    }
)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _last_tick_ts(root: Path) -> datetime | None:
    """Return the timestamp of the most recent tick event, or None.

    Reads ``state/events/ticks.ndjson`` from the tail; the last well-formed
    record with a ``ts`` wins. Unlike the windowed detectors this does NOT
    take a ``since`` — the whole point is to detect that ticks STOPPED.
    """
    stream = root / "state" / "events" / "ticks.ndjson"
    if not stream.exists():
        return None
    last: datetime | None = None
    try:
        with stream.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(rec.get("ts"))
                if ts is not None:
                    last = ts
    except OSError:
        return None
    return last


def stalled_stories(
    *,
    root: Path,
    now: datetime | None = None,
    in_progress_stall_minutes: float = 30.0,
    stall_minutes: float = 120.0,
    tick_silence_minutes: float = 15.0,
) -> dict:
    """Return absolute liveness observations for the chain.

    Parameters
    ----------
    root:
        Factory root directory.
    now:
        Reference time (defaults to ``datetime.now(UTC)``). Injectable for
        deterministic tests.
    in_progress_stall_minutes:
        Age above which a ``*_in_progress`` story is reported as
        ``stuck_in_progress`` (a handler that never returned).
    stall_minutes:
        Age above which any non-terminal, non-deployed story is reported as
        ``stalled`` (no forward progress).
    tick_silence_minutes:
        Minutes since the last tick above which ``no_tick_recently`` fires.

    Returns
    -------
    dict
        ``{"alarms": [...], "stuck_in_progress": [...], "stalled": [...],
        "minutes_since_last_tick": float|None, "no_tick_recently": bool,
        "non_terminal_total": int, "now": iso}``

        ``alarms`` is a short list of human-readable alarm strings; an empty
        list means "no liveness problem detected." Each ``stuck_in_progress`` /
        ``stalled`` entry is ``{"story_id", "state", "app", "slug",
        "age_minutes"}``.
    """
    now = now or datetime.now(UTC)
    db_path = root / "state" / "factory.db"

    stuck_in_progress: list[dict] = []
    stalled: list[dict] = []
    non_terminal_total = 0
    minutes_since_any_story_update: float | None = None

    if db_path.exists():
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute(
                "SELECT id, state, app, slug, updated_at FROM stories"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            if conn is not None:
                conn.close()

        for story_id, state, app, slug, updated_at in rows:
            ts = _parse_ts(updated_at)
            if ts is not None:
                age_min_any = (now - ts).total_seconds() / 60.0
                if (
                    minutes_since_any_story_update is None
                    or age_min_any < minutes_since_any_story_update
                ):
                    minutes_since_any_story_update = round(age_min_any, 1)
            if state in _TERMINAL_STATES:
                continue
            non_terminal_total += 1
            if ts is None:
                continue
            age_min = (now - ts).total_seconds() / 60.0
            entry = {
                "story_id": story_id,
                "state": state,
                "app": app,
                "slug": slug,
                "age_minutes": round(age_min, 1),
            }
            if str(state).endswith("_in_progress") and age_min >= in_progress_stall_minutes:
                stuck_in_progress.append(entry)
            elif age_min >= stall_minutes:
                stalled.append(entry)

    last_tick = _last_tick_ts(root)
    minutes_since_last_tick: float | None = None
    if last_tick is not None:
        minutes_since_last_tick = round((now - last_tick).total_seconds() / 60.0, 1)
    no_tick_recently = (
        minutes_since_last_tick is not None
        and minutes_since_last_tick >= tick_silence_minutes
    )

    alarms: list[str] = []
    if stuck_in_progress:
        alarms.append(
            f"{len(stuck_in_progress)} story(ies) stuck >{in_progress_stall_minutes:g}m "
            f"in a *_in_progress state (handler never returned): "
            + ", ".join(f"#{e['story_id']}@{e['state']}" for e in stuck_in_progress[:10])
        )
    # An aged backlog is only an ALARM when the factory is actually idle.
    # While the chain drains a large queue serially, the oldest stories'
    # updated_at keeps aging even though work is flowing — a truly stuck
    # factory shows NO recent story updates AND/OR no recent ticks. Alarming
    # on "old stories exist while the factory is visibly working" caused an
    # L1->L2->L3 churn loop on every watcher cycle (2026-06-11, ~$2/hour of
    # duplicate halt-urgency concerns during a healthy drain).
    draining = (
        not no_tick_recently
        and minutes_since_any_story_update is not None
        and minutes_since_any_story_update < in_progress_stall_minutes
    )
    if stalled and not draining:
        alarms.append(
            f"{len(stalled)} non-terminal story(ies) with no state change in "
            f">{stall_minutes:g}m: "
            + ", ".join(f"#{e['story_id']}@{e['state']}" for e in stalled[:10])
        )
    if no_tick_recently:
        alarms.append(
            f"no orchestrator tick in {minutes_since_last_tick:g}m "
            f"(>{tick_silence_minutes:g}m) — the drive loop is likely dead; "
            f"nothing is being dispatched."
        )

    return {
        "alarms": alarms,
        "stuck_in_progress": stuck_in_progress,
        "stalled": stalled,
        "minutes_since_last_tick": minutes_since_last_tick,
        "no_tick_recently": no_tick_recently,
        "non_terminal_total": non_terminal_total,
        "minutes_since_any_story_update": minutes_since_any_story_update,
        "draining": draining,
        "now": now.isoformat(),
    }
