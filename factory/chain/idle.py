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

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, create_engine, select

_IDLE_LABEL = "factory-idle"

# --------------------------------------------------------------------------- #
# Idle -> generate-work (Ceiling A: keep the factory productively busy)
# --------------------------------------------------------------------------- #

# Work-generating personas the idle path rotates through when an app is
# drained. Ordered by cost/breadth: bug_hunter and ux_auditor produce the
# most developable directions; security is narrower. ralph is deliberately
# excluded — it runs hourly on cron already and is drift-detection, not
# net-new-work generation.
_IDLE_GENERATORS: tuple[str, ...] = ("bug_hunter", "ux_auditor", "security")

# Anti-thrash: the tick fires every ~5 min, and a drained app is idle on
# every one of them. Without a cooldown the idle path would burn every
# generator's daily cap within the first half-hour and then emit nothing but
# ``all_capped`` for the rest of the day. A multi-hour cooldown spreads the
# generator runs across the day so the backlog refills steadily. The daily
# per-persona caps (``run_scheduled_persona`` enforces them) remain the hard
# ceiling; the cooldown only paces the fires.
_IDLE_GEN_COOLDOWN_HOURS = 6.0

# Per-app pacing/rotation marker. Small JSON keyed by app:
#   {"<app>": {"last_fired": "<iso>", "next_index": <int>}}
_IDLE_GEN_STATE_FILE = "idle_generator.json"

# Sentinel so ``maybe_generate_idle_work`` can tell "caller didn't pass a
# snapshot, compute one" apart from "caller passed None, meaning not idle".
_UNSET: Any = object()


@dataclass
class IdleWorkResult:
    """Outcome of one idle-triggered work-generation attempt."""

    fired: bool
    persona: str | None = None
    reason: str = ""
    directions_filed: int = 0
    findings_count: int = 0
    status: str | None = None


def _idle_gen_state_path(software_factory_root: Path) -> Path:
    return Path(software_factory_root) / "state" / _IDLE_GEN_STATE_FILE


def _load_idle_gen_state(software_factory_root: Path) -> dict[str, Any]:
    """Read the per-app idle-generation marker; tolerant of a missing/corrupt file."""
    path = _idle_gen_state_path(software_factory_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_idle_gen_state(software_factory_root: Path, state: dict[str, Any]) -> None:
    """Persist the marker; best-effort (a write hiccup must never fail a tick)."""
    path = _idle_gen_state_path(software_factory_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:  # noqa: BLE001 - marker persistence is best-effort
        return


def maybe_generate_idle_work(
    app: str,
    software_factory_root: Path,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    cooldown_hours: float = _IDLE_GEN_COOLDOWN_HOURS,
    generators: tuple[str, ...] = _IDLE_GENERATORS,
    since_hours: int = 2,
    dispatch_fn: Callable[..., Any] | None = None,
    idle_snapshot: Any = _UNSET,
) -> IdleWorkResult:
    """When ``app`` is drained, dispatch a work-generating persona on demand.

    This is the on-demand counterpart to the scheduled-cron path: rather than
    wait hours for the next bug_hunt/ux_audit slot, a genuinely idle app
    refills its own backlog immediately. The loop then closes with no
    operator: the persona files ``explore``-tagged directions (see
    ``scheduled_tasks._file_finding_as_direction``) which the same/next tick's
    ``auto_pm_sync`` decomposes into stories.

    Guardrails:

      * **Idle gate** — only fires when ``detect_idle`` says the app is
        drained (no in-flight stories, no recent findings, no recent
        deploys). If ``idle_snapshot`` is passed it is trusted (the tick
        computes it once for the ``app_idle`` event and reuses it here).
      * **Daily caps** — reuses ``run_scheduled_persona``, which enforces the
        per-persona ``rate_limits.<persona>_runs_per_day`` cap and returns
        ``status="rejected"`` when hit. A capped persona is skipped and the
        next in the rotation is tried; if all are capped nothing fires.
      * **Cooldown** — at most one generator run per ``cooldown_hours`` per
        app, so a drained app doesn't burn every cap in minutes.
      * **Rotation** — advances through ``generators`` across fires so the
        backlog gets varied work (bugs, UX, security), not a monoculture.

    Returns an ``IdleWorkResult``. ``fired=False`` with ``reason`` in
    {``not_idle``, ``cooldown``, ``all_capped``} means nothing dispatched.
    """
    root = Path(software_factory_root)
    moment = now or datetime.now(UTC)

    # Self-tick guard (Tier 3 — FACTORY-SELF-TICK). When ``app`` builds the
    # factory's own repo, idle-generation only fires if self-tick is explicitly
    # enabled. Otherwise the factory would burn generator budget filing
    # factory-improvement directions that pm-sync (also self-tick-gated) refuses
    # to decompose into stories. Best-effort: if the config can't be read we
    # fall through (a non-factory app is never affected).
    try:
        from factory.app_config import load_app_config, targets_factory_repo

        _cfg = load_app_config(app, root)
        if targets_factory_repo(_cfg.repo) and not _cfg.self_tick_enabled:
            return IdleWorkResult(fired=False, reason="self_tick_disabled")
    except Exception:  # noqa: BLE001 - missing/broken config → not a gated factory app
        pass

    if idle_snapshot is _UNSET:
        idle_snapshot = detect_idle(app, root, since_hours=since_hours)
    if idle_snapshot is None:
        return IdleWorkResult(fired=False, reason="not_idle")

    if not generators:
        return IdleWorkResult(fired=False, reason="no_generators")

    state = _load_idle_gen_state(root)
    raw_app_state = state.get(app)
    app_state: dict[str, Any] = raw_app_state if isinstance(raw_app_state, dict) else {}

    last_fired = app_state.get("last_fired")
    if last_fired:
        try:
            lf = datetime.fromisoformat(str(last_fired))
            if lf.tzinfo is None:
                lf = lf.replace(tzinfo=UTC)
            if moment - lf < timedelta(hours=cooldown_hours):
                return IdleWorkResult(fired=False, reason="cooldown")
        except Exception:  # noqa: BLE001 - a corrupt timestamp shouldn't block generation
            pass

    if dispatch_fn is None:
        from factory.chain.scheduled_tasks import run_scheduled_persona

        dispatch_fn = run_scheduled_persona

    n = len(generators)
    start_index = 0
    try:
        start_index = int(app_state.get("next_index", 0)) % n
    except Exception:  # noqa: BLE001 - corrupt index falls back to 0
        start_index = 0

    for offset in range(n):
        idx = (start_index + offset) % n
        persona = generators[idx]
        out = dispatch_fn(persona, app, root, dry_run=dry_run)
        status = getattr(out, "status", None)
        # ``rejected`` means the daily cap for this persona is hit — try the
        # next one in the rotation rather than giving up.
        if status == "rejected":
            continue
        # Anything else (ok / dry_run / errored) counts as a fire: record the
        # cooldown + advance the rotation so we don't re-dispatch on the very
        # next tick. Treating ``errored`` as a fire is deliberate — it stops
        # an error storm from a persistently-failing persona.
        app_state = {"last_fired": moment.isoformat(), "next_index": (idx + 1) % n}
        state[app] = app_state
        if not dry_run:
            _save_idle_gen_state(root, state)
        return IdleWorkResult(
            fired=True,
            persona=persona,
            reason="dispatched",
            directions_filed=len(getattr(out, "directions_filed", []) or []),
            findings_count=int(getattr(out, "findings_count", 0) or 0),
            status=status,
        )

    return IdleWorkResult(fired=False, reason="all_capped")


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
