"""DB schema for the observability subsystem.

Two new tables (``live_handlers``, ``handler_baselines``) plus idempotent
ALTERs that add columns onto pre-existing ``runs`` and ``stories`` tables
without dropping data.

The migration helper runs at TUI startup and inside the runner so existing
state DBs upgrade transparently on the next factory invocation. SQLite ALTER
TABLE only supports ADD COLUMN, which is enough for our purposes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlmodel import Field, SQLModel


class LiveHandler(SQLModel, table=True):
    """A handler that is *currently executing*.

    Inserted on entry to ``sandbox_run`` / ``text_run`` and deleted on exit.
    The TUI polls this table to show what each persona is doing right now —
    elapsed = ``now - started_at``. Rows from dead processes are reaped by
    ``reap_stale_heartbeats`` (any row whose ``pid`` is no longer alive).
    """

    __tablename__ = "live_handlers"

    id: int | None = Field(default=None, primary_key=True)
    started_at: str = Field(index=True)
    persona: str = Field(index=True)
    model: str
    mode: str
    story_id: int | None = Field(default=None, index=True)
    app: str | None = Field(default=None, index=True)
    direction_id: str | None = Field(default=None, index=True)
    pid: int


class HandlerBaseline(SQLModel, table=True):
    """Median wall-clock seconds per (persona, points) bucket.

    Recomputed periodically from completed runs by
    ``estimator.recompute_baselines``. The Monte Carlo ETA reads this to
    seed each remaining handler's expected duration; velocity samples
    then perturb it per simulation run.
    """

    __tablename__ = "handler_baselines"

    id: int | None = Field(default=None, primary_key=True)
    persona: str = Field(index=True)
    points: int = Field(index=True)
    median_seconds: float
    sample_count: int
    updated_at: str


_RUNS_NEW_COLUMNS: list[tuple[str, str]] = [
    ("duration_s", "REAL"),
    ("story_id", "INTEGER"),
    ("model_tier", "VARCHAR"),
]

_STORIES_NEW_COLUMNS: list[tuple[str, str]] = [
    ("points", "INTEGER"),
    ("estimated_seconds", "REAL"),
]


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def _ensure_columns(
    conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]
) -> None:
    existing = _existing_columns(conn, table)
    for name, sql_type in columns:
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def migrate(db_path: Path) -> None:
    """Run idempotent schema migrations against ``db_path``.

    Adds new columns onto ``runs`` and ``stories`` if missing, and ensures
    the new ``live_handlers`` / ``handler_baselines`` tables exist via
    ``SQLModel.metadata.create_all``. Safe to call on every CLI entry.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "runs" in existing_tables:
            _ensure_columns(conn, "runs", _RUNS_NEW_COLUMNS)
        if "stories" in existing_tables:
            _ensure_columns(conn, "stories", _STORIES_NEW_COLUMNS)
        conn.commit()
    finally:
        conn.close()

    from sqlmodel import create_engine

    eng = create_engine(f"sqlite:///{db_path}", echo=False)
    SQLModel.metadata.create_all(eng)
