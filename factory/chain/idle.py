"""Phase 7 — idle detection + ``factory-idle`` GitHub issue.

A factory is "idle" for app ``X`` when ALL of the following hold:

  * the queue (in-flight stories) is empty
  * no scheduled persona run for the app produced findings within the
    last ``since_hours`` hours
  * no deploys have completed (any status) within the last
    ``since_hours`` hours

When idle, the factory opens (or updates) a single GH issue labeled
``factory-idle`` with a body listing the last 5 directions for context.
Idempotent: re-running while the issue is still open updates the body
in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, create_engine, select

_IDLE_LABEL = "factory-idle"


@dataclass
class IdleSnapshot:
    """A point-in-time idle determination for a single app."""

    app: str
    idle_since: datetime
    # ``parse_direction_dir`` returns ``Direction`` records but we don't
    # want the heavy import at module import time, so the field carries
    # ``Any`` and the caller treats them as opaque records with ``id``,
    # ``slug``, ``title`` attributes.
    recent_directions: list[Any] = field(default_factory=list)


def _stories_in_flight(software_factory_root: Path, app: str) -> int:
    """Count stories for ``app`` whose state is neither terminal nor blocked.

    "Terminal" here mirrors the ``factory queue`` convention: anything
    past ``pr_open`` (including the blocked-for-deploy state) doesn't
    count as work the factory is actively chewing on. A blocked-for-
    clarification story doesn't block idle either — it's waiting on the
    user, exactly the case ``factory-idle`` wants to surface.
    """
    from factory.chain.factory_status import _TERMINAL_STATES
    from factory.chain.state_machine import StoryRecord

    db = software_factory_root / "state" / "factory.db"
    if not db.exists():
        return 0
    eng = create_engine(f"sqlite:///{db}", echo=False)
    try:
        with Session(eng) as session:
            rows = session.exec(select(StoryRecord).where(StoryRecord.app == app)).all()
    except Exception:
        # Fresh DB with no stories table yet.
        return 0
    return sum(1 for r in rows if r.state not in _TERMINAL_STATES)


def _recent_findings(software_factory_root: Path, app: str, *, since_hours: int) -> int:
    """Count ScheduledRunRecord rows with non-zero findings in the window."""
    from factory.chain.scheduled_tasks import ScheduledRunRecord

    db = software_factory_root / "state" / "factory.db"
    if not db.exists():
        return 0
    eng = create_engine(f"sqlite:///{db}", echo=False)
    cutoff = (datetime.now(UTC) - timedelta(hours=since_hours)).isoformat()
    try:
        with Session(eng) as session:
            rows = session.exec(
                select(ScheduledRunRecord).where(
                    ScheduledRunRecord.app == app,
                    ScheduledRunRecord.ts >= cutoff,
                )
            ).all()
    except Exception:
        return 0
    return sum(1 for r in rows if (r.findings_count or 0) > 0)


def _recent_deploys(software_factory_root: Path, app: str, *, since_hours: int) -> int:
    """Count DeployActionRecord rows (any status) in the window."""
    from factory.deploy.models import DeployActionRecord

    db = software_factory_root / "state" / "factory.db"
    if not db.exists():
        return 0
    eng = create_engine(f"sqlite:///{db}", echo=False)
    cutoff = (datetime.now(UTC) - timedelta(hours=since_hours)).isoformat()
    try:
        with Session(eng) as session:
            rows = list(
                session.exec(
                    select(DeployActionRecord).where(
                        DeployActionRecord.app == app,
                        DeployActionRecord.ts >= cutoff,
                    )
                ).all()
            )
    except Exception:
        return 0
    return len(rows)


def _last_n_directions(app: str, software_factory_root: Path, *, n: int = 5) -> list[Any]:
    """Return the last ``n`` directions for ``app``, sorted newest first.

    "Newest" means by directory mtime — directions/<id>-<slug> directories
    are immutable on disk after PM-sync writes ``state.yaml``, so mtime
    is a reasonable proxy when the id-prefix isn't monotonic across
    parallel branches.
    """
    from factory.directions.parser import list_direction_dirs, parse_direction_dir

    dirs = list_direction_dirs(app, software_factory_root)
    # Newest by directory mtime; tie-break by id descending.
    dirs.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    out: list[Any] = []
    for ddir in dirs[:n]:
        try:
            out.append(parse_direction_dir(app, ddir))
        except Exception:
            continue
    return out


def detect_idle(
    app: str,
    software_factory_root: Path,
    *,
    since_hours: int = 2,
) -> IdleSnapshot | None:
    """Return an ``IdleSnapshot`` iff the factory is idle for ``app``.

    Returns ``None`` if any of: stories are in flight, scheduled persona
    findings landed in the window, or a deploy completed in the window.
    """
    software_factory_root = Path(software_factory_root)

    if _stories_in_flight(software_factory_root, app) > 0:
        return None
    if _recent_findings(software_factory_root, app, since_hours=since_hours) > 0:
        return None
    if _recent_deploys(software_factory_root, app, since_hours=since_hours) > 0:
        return None

    return IdleSnapshot(
        app=app,
        idle_since=datetime.now(UTC),
        recent_directions=_last_n_directions(app, software_factory_root, n=5),
    )


def _compose_idle_body(snapshot: IdleSnapshot) -> str:
    """Build the markdown body for the ``factory-idle`` issue."""
    parts: list[str] = []
    parts.append(f"## Factory idle for `{snapshot.app}`")
    parts.append("")
    parts.append(
        f"No work in flight, no scheduler findings, no deploys "
        f"since `{snapshot.idle_since.isoformat()}`."
    )
    parts.append("")
    parts.append("### Recent directions (last 5)")
    if snapshot.recent_directions:
        for d in snapshot.recent_directions:
            iss = ""
            tracker = (getattr(d, "state", {}) or {}).get("tracker_issue")
            if tracker:
                iss = f" — tracker #{tracker}"
            parts.append(f"- `{d.id}-{d.slug}` — {d.title}{iss}")
    else:
        parts.append("_(no directions yet)_")
    parts.append("")
    parts.append("### What's next?")
    parts.append("Pick one of:")
    parts.append("- Open a new direction: `factory new-direction --app <app>`")
    parts.append('- Pipe a thought: `factory tell --app <app> "..."`')
    parts.append("- File a direction issue on GitHub with the `direction` label.")
    parts.append("")
    parts.append("---")
    parts.append("_This issue is maintained by the factory. Close it to dismiss._")
    return "\n".join(parts)


def _find_open_idle_issue(repo: Any) -> Any | None:
    """Return the first OPEN issue labeled ``factory-idle`` (or None).

    pygithub-style API: ``repo.get_issues(state='open', labels=[...])``
    returns an iterable. We tolerate missing labels= support by falling
    back to a manual scan.
    """
    try:
        candidates = repo.get_issues(state="open", labels=[_IDLE_LABEL])
        for issue in candidates:
            return issue
    except TypeError:
        # Fake or mock client that doesn't accept labels=
        try:
            for issue in repo.get_issues(state="open"):
                labels = [
                    lbl.name if hasattr(lbl, "name") else str(lbl) for lbl in (issue.labels or [])
                ]
                if _IDLE_LABEL in labels:
                    return issue
        except Exception:
            return None
    except Exception:
        return None
    return None


def open_idle_issue(
    snapshot: IdleSnapshot,
    github_client: Any,
    *,
    software_factory_root: Path,
    app_config: Any = None,
) -> int:
    """Open (or update) the ``factory-idle`` issue for ``snapshot.app``.

    If an OPEN issue labeled ``factory-idle`` already exists, its body
    is updated. Otherwise a new issue is created. Returns the issue
    number.
    """
    if github_client is None:
        raise ValueError("github_client is required for open_idle_issue")

    if app_config is None:
        from factory.app_config import load_app_config

        app_config = load_app_config(snapshot.app, software_factory_root)

    repo = github_client.get_repo(app_config.repo)
    title = f"[FACTORY] What's next for {snapshot.app}?"
    body = _compose_idle_body(snapshot)
    labels = [_IDLE_LABEL]

    existing = _find_open_idle_issue(repo)
    if existing is not None:
        existing.edit(title=title, body=body, labels=labels)
        return int(existing.number)

    issue = repo.create_issue(title=title, body=body, labels=labels)
    return int(issue.number)
