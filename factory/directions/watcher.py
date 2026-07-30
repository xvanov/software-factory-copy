"""Per-app direction queue management.

Status resolution precedence:
1. ``directions`` table row (when one exists for the app + direction_id).
2. On-disk ``state.yaml`` ``status`` field.
3. ``created`` (default for a hand-created direction with no DB row).

The ``DirectionCursor`` SQLModel table is a *cursor optimization* — it
remembers the highest direction id we've seen for an app so the watcher can
skip already-processed entries without a full disk scan in steady state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlmodel import Field, Session, SQLModel, create_engine, select

from factory.directions.parser import Direction, list_direction_dirs, parse_direction_dir
from factory.directions.schema import get_direction


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


def _resolve_status(
    app: str,
    direction_id: str,
    state_yaml_status: str,
    engine: Any,
) -> str:
    """Resolve direction status with DB-first precedence.

    1. DB row → row.status
    2. state.yaml → *state_yaml_status*
    3. ``created``
    """
    with Session(engine) as session:
        row = get_direction(session, app, direction_id)
        if row is not None:
            return row.status
    # state_yaml_status is already the parsed state.yaml value, or "created"
    # if state.yaml was missing/corrupt — see parse_direction_dir.
    return state_yaml_status


def pending_directions(
    app: str, software_factory_root: Path, state_db_path: Path
) -> list[Direction]:
    """Return parsed ``Direction`` records whose status indicates the chain has
    not yet validated them.

    Status resolution: ``directions`` table row → ``state.yaml`` → ``created``.
    Pending statuses: ``created``, ``needs-direction``.
    """
    engine = _engine(state_db_path)  # ensure tables exist for callers downstream
    out: list[Direction] = []
    for dir_path in list_direction_dirs(app, software_factory_root):
        d = parse_direction_dir(app, dir_path, software_factory_root=software_factory_root)
        # Resolve status from DB first, falling back to state.yaml / created.
        d.status = _resolve_status(app, d.id, d.status, engine)
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
