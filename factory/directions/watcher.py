"""Per-app direction queue management.

The truth is the on-disk ``state.yaml`` ``status`` field; the
``DirectionCursor`` SQLModel table is a *cursor optimization* — it remembers
the highest direction id we've seen for an app so the watcher can skip
already-processed entries without a full disk scan in steady state. Phase 1
just uses the disk-truth path; the cursor is wired for Phase 2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlmodel import Field, Session, SQLModel, create_engine, select

from factory.directions.parser import Direction, list_direction_dirs, parse_direction_dir


class DirectionCursor(SQLModel, table=True):
    """Per-app cursor over directions/. Optional optimization, see module docstring."""

    __tablename__ = "direction_cursors"

    id: int | None = Field(default=None, primary_key=True)
    app: str = Field(unique=True, index=True)
    last_seen_direction_id: str = ""
    updated_at: str = ""


def _engine(db_path: Path) -> Any:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    SQLModel.metadata.create_all(engine)
    return engine


def pending_directions(
    app: str, software_factory_root: Path, state_db_path: Path
) -> list[Direction]:
    """Return parsed ``Direction`` records whose ``state.yaml.status`` indicates
    the chain has not yet validated them.

    Phase 1 pending statuses: ``created`` (just made), ``needs-direction``
    (re-process whenever updated; the chain will mark it ``pm-validated`` when
    backpressure is sufficient).
    """
    _ = _engine(state_db_path)  # ensure table exists for callers downstream
    out: list[Direction] = []
    for dir_path in list_direction_dirs(app, software_factory_root):
        d = parse_direction_dir(
            app, dir_path, software_factory_root=software_factory_root
        )
        if d.status in {"created", "needs-direction"}:
            out.append(d)
    return out


def mark_direction_status(
    direction: Direction,
    new_status: str,
    *,
    by: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Update ``state.yaml`` in-place: set ``status``, append an audit entry.

    Preserves any other keys in state.yaml (e.g. ``pm_result``, ``tracker_issue``).
    """
    state_path = Path(direction.dir_path) / "state.yaml"
    if state_path.exists():
        try:
            state = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
            if not isinstance(state, dict):
                state = {}
        except yaml.YAMLError:
            state = {}
    else:
        state = {}

    state["status"] = new_status
    audit = state.get("audit") or []
    if not isinstance(audit, list):
        audit = []
    audit.append(
        {
            "ts": datetime.now(UTC).isoformat(),
            "by": by,
            "event": f"status -> {new_status}",
            "details": details or {},
        }
    )
    state["audit"] = audit
    state_path.write_text(yaml.safe_dump(state, sort_keys=False), encoding="utf-8")

    # Keep the in-memory record in sync.
    direction.status = new_status
    direction.state = state


def merge_state(direction: Direction, patch: dict[str, Any]) -> None:
    """Merge ``patch`` into ``state.yaml`` at the top level (shallow merge)."""
    state_path = Path(direction.dir_path) / "state.yaml"
    if state_path.exists():
        try:
            state = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
            if not isinstance(state, dict):
                state = {}
        except yaml.YAMLError:
            state = {}
    else:
        state = {}
    state.update(patch)
    state_path.write_text(yaml.safe_dump(state, sort_keys=False), encoding="utf-8")
    direction.state = state


def bump_cursor(app: str, last_id: str, state_db_path: Path) -> None:
    """Persist the highest direction id we've processed for ``app``."""
    engine = _engine(state_db_path)
    now = datetime.now(UTC).isoformat()
    with Session(engine) as session:
        existing = session.exec(select(DirectionCursor).where(DirectionCursor.app == app)).first()
        if existing is None:
            session.add(DirectionCursor(app=app, last_seen_direction_id=last_id, updated_at=now))
        else:
            existing.last_seen_direction_id = last_id
            existing.updated_at = now
            session.add(existing)
        session.commit()


def get_cursor(app: str, state_db_path: Path) -> str | None:
    """Return the last-seen direction id for ``app``, or None."""
    engine = _engine(state_db_path)
    with Session(engine) as session:
        existing = session.exec(select(DirectionCursor).where(DirectionCursor.app == app)).first()
        if existing is None:
            return None
        return existing.last_seen_direction_id
