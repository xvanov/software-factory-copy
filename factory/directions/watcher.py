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
    """Authoritative database write + best-effort ``state.yaml`` projection.

    The ``directions`` table row is the source of truth.  ``state.yaml`` is
    still written for human inspection but its failure does NOT fail the
    transition.
    """
    # ---- authoritative database write -----------------------------------
    root = _root_from_direction(direction)
    db_path = root / "state" / "factory.db"
    engine = _engine(db_path)

    tracker_issue: int | None = None
    raw = (direction.state or {}).get("tracker_issue")
    if isinstance(raw, int) and raw > 0:
        tracker_issue = raw

    from factory.directions.schema import upsert_direction

    with Session(engine) as session:
        upsert_direction(
            session,
            app=direction.app,
            direction_id=direction.id,
            slug=direction.slug,
            status=new_status,
            tracker_issue=tracker_issue,
            updated_by=by,
        )

    # ---- best-effort state.yaml projection ------------------------------
    state_path = Path(direction.dir_path) / "state.yaml"
    state = _read_state_yaml(state_path)

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

    try:
        state_path.write_text(yaml.safe_dump(state, sort_keys=False), encoding="utf-8")
    except OSError:
        # File projection is best-effort; the DB row is already committed.
        pass

    # Keep the in-memory record in sync with the authoritative write.
    direction.status = new_status
    direction.state = state


def _read_state_yaml(state_path: Path) -> dict[str, Any]:
    """Read and return the existing ``state.yaml`` dict, or an empty dict.

    Robust against missing file, permission errors, and corrupt YAML.
    """
    try:
        if state_path.exists() and state_path.is_file():
            state = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
            if not isinstance(state, dict):
                state = {}
            return state
    except (yaml.YAMLError, OSError):
        pass
    return {}


def _root_from_direction(direction: Direction) -> Path:
    """Derive the software-factory root from the direction's directory path.

    Direction dir_path layout: ``<root>/apps/<app>/directions/<id>-<slug>/``
    so the root is 3 levels up from *dir_path*.
    """
    return direction.dir_path.resolve().parents[3]


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
