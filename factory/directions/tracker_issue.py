"""Direction Tracker GitHub issue — open/update + needs-direction comments.

The tracker is the *one issue per direction* the factory keeps current with
links to child stories, current status, and any blockers. Idempotent: a
direction's ``state.yaml`` carries ``tracker_issue: <number>`` once an issue
exists, and subsequent calls update that issue in place.

The ``github_client`` parameter is the ``pygithub.Github`` object (or a
duck-type mock for tests). We don't construct it here — the caller wires
authentication so the same client can be reused across calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from factory.app_config import AppConfig
from factory.directions.parser import Direction, MissingDirection, resolve_direction_chain
from factory.directions.watcher import merge_state

_TRACKER_LABEL = "direction-tracker"
_NEEDS_DIRECTION_LABEL = "needs-direction"


def _format_tracker_body(
    direction: Direction,
    *,
    pm_summary: str | None,
    child_issue_numbers: list[int],
    extra_sections: list[str] | None = None,
    direction_chain: list[Direction | MissingDirection] | None = None,
) -> str:
    parts: list[str] = []

    if direction_chain and len(direction_chain) > 1:
        chain_parts: list[str] = []
        for item in direction_chain:
            if isinstance(item, MissingDirection):
                chain_parts.append(f"`{item.id_slug}`")
            elif item.id_slug == direction.id_slug:
                chain_parts.append("**THIS**")
            else:
                tracker_num = item.state.get("tracker_issue") if item.state else None
                if isinstance(tracker_num, int) and tracker_num > 0:
                    chain_parts.append(f"`{item.id_slug}` #{tracker_num}")
                else:
                    chain_parts.append(f"`{item.id_slug}`")
        parts.append(f"**Chain:** {' ← '.join(chain_parts)}")
        parts.append("")

    parts.append(f"**Direction:** `{direction.id}-{direction.slug}`")
    parts.append(f"**App:** `{direction.app}`")
    if direction.type_tag:
        parts.append(f"**Type:** `{direction.type_tag}`")
    parts.append(f"**Status:** `{direction.status}`")
    parts.append("")
    if pm_summary:
        parts.append("## Summary")
        parts.append(pm_summary.strip())
        parts.append("")
    parts.append("## Child stories")
    if child_issue_numbers:
        for n in child_issue_numbers:
            parts.append(f"- #{n}")
    else:
        parts.append("_(no child stories yet)_")
    parts.append("")
    if extra_sections:
        for section in extra_sections:
            parts.append(section.rstrip())
            parts.append("")
    parts.append("---")
    parts.append("_This issue is maintained by the factory. Edits will be overwritten._")
    return "\n".join(parts)


def _build_labels(direction: Direction, pm_labels: list[str]) -> list[str]:
    labels = {_TRACKER_LABEL}
    if direction.type_tag:
        labels.add(direction.type_tag)
    for lbl in pm_labels:
        if lbl:
            labels.add(lbl)
    return sorted(labels)


def open_or_update_tracker_issue(
    direction: Direction,
    app_config: AppConfig,
    github_client: Any,
    *,
    pm_result: dict[str, Any] | None = None,
    child_issue_numbers: list[int] | None = None,
    software_factory_root: Path | None = None,
) -> int:
    """Idempotently open or update the Direction Tracker issue.

    Returns the issue number. Persists the number into ``state.yaml`` under
    ``tracker_issue`` on first creation; subsequent calls re-use it.
    """
    repo = github_client.get_repo(app_config.repo)
    pm_result = pm_result or {}
    child_issue_numbers = child_issue_numbers or []

    title = pm_result.get("tracker_title") or f"[DIRECTION] {direction.title}"
    if not title.startswith("[DIRECTION]"):
        title = f"[DIRECTION] {title}"
    if len(title) > 256:
        title = title[:253] + "..."

    pm_body = pm_result.get("tracker_body")
    pm_labels: list[str] = list(pm_result.get("labels") or [])
    priority = pm_result.get("priority")
    if priority and not any(lbl.startswith("priority/") for lbl in pm_labels):
        pm_labels.append(f"priority/{priority}")

    chain: list[Direction | MissingDirection] | None = None
    if software_factory_root is not None and direction.parent_direction:
        chain = resolve_direction_chain(direction, software_factory_root)

    body = _format_tracker_body(
        direction,
        pm_summary=pm_body,
        child_issue_numbers=child_issue_numbers,
        direction_chain=chain,
    )
    labels = _build_labels(direction, pm_labels)

    existing_number = direction.state.get("tracker_issue") if direction.state else None
    if isinstance(existing_number, int) and existing_number > 0:
        issue = repo.get_issue(existing_number)
        issue.edit(title=title, body=body, labels=labels)
        return existing_number

    issue = repo.create_issue(title=title, body=body, labels=labels)
    number = int(issue.number)
    merge_state(direction, {"tracker_issue": number})
    return number


def record_needs_direction(
    direction: Direction,
    missing: list[str],
    app_config: AppConfig,
    github_client: Any,
    *,
    pm_result: dict[str, Any] | None = None,
) -> int:
    """Open/update the tracker issue with the ``needs-direction`` label + comment.

    Direction status stays ``created`` / ``needs-direction`` so the watcher
    picks it up again after the user updates the on-disk direction.
    """
    pm_result = pm_result or {}
    extra_labels = list(pm_result.get("labels") or [])
    if _NEEDS_DIRECTION_LABEL not in extra_labels:
        extra_labels.append(_NEEDS_DIRECTION_LABEL)
    merged_pm = dict(pm_result)
    merged_pm["labels"] = extra_labels

    issue_number = open_or_update_tracker_issue(
        direction,
        app_config,
        github_client,
        pm_result=merged_pm,
        child_issue_numbers=[],
    )

    repo = github_client.get_repo(app_config.repo)
    issue = repo.get_issue(issue_number)
    missing_text = ", ".join(missing) if missing else "(unspecified)"
    comment_body = (
        f"**Needs direction.** Missing: {missing_text}.\n\n"
        "Add the missing artifact(s) (flow.md / api_spec.md / acceptance "
        "criteria) to the direction directory and the factory will re-validate "
        "on the next pm-sync."
    )
    # Idempotent: re-validating an unchanged direction must not append the
    # same comment again — repeated pm-sync passes were spamming one
    # identical comment per pass onto every needs-direction tracker issue.
    if _last_comment_body(issue) != comment_body:
        issue.create_comment(comment_body)
    return issue_number


def _last_comment_body(issue: Any) -> str | None:
    """Best-effort body of the issue's most recent comment (None on failure)."""
    try:
        comments = issue.get_comments()
        total = getattr(comments, "totalCount", None)
        if total is None:
            seq = list(comments)
            return getattr(seq[-1], "body", None) if seq else None
        if total == 0:
            return None
        return getattr(comments[total - 1], "body", None)
    except Exception:  # noqa: BLE001 - never block the comment on a read error
        return None


def close_story_issue(
    story: Any,
    app_config: AppConfig,
    github_client: Any,
) -> bool:
    """Close a story's GitHub issue after it deploys. Idempotent, best-effort.

    The chain sets a deployed story's DB state but historically never closed
    its issue, so per-story ``story`` issues accumulated open forever (audit
    2026-07-18: 53 of them). Returns True if a close was attempted. Any GH
    error is swallowed — a bookkeeping close must never break the deploy path.
    """
    num = getattr(story, "github_issue_number", None)
    if not num:
        return False
    try:
        repo = github_client.get_repo(app_config.repo)
        issue = repo.get_issue(int(num))
        if str(getattr(issue, "state", "")).lower() == "closed":
            return False
        issue.create_comment(
            "✅ Deployed — closing automatically (story reached DEPLOYED in the chain)."
        )
        issue.edit(state="closed")
        return True
    except Exception:  # noqa: BLE001 - never break deploy on a bookkeeping close
        return False


# Story states that RESOLVE a child story for the purpose of closing a
# direction tracker. Deliberately an explicit allowlist (not ``is_terminal``):
#   DEPLOYED             = shipped;
#   SUPERSEDED_BY_SIBLING = the dual-draft loser (never ships, by design);
#   "closed"             = a regime-invalidated / abandoned story row.
# Everything else keeps the tracker OPEN — including ``BLOCKED_*`` (needs a
# human) AND states like ``CI_PENDING`` that are "terminal-by-omission" in the
# state machine (the auto-merge poller drives ``ci_pending -> ci_green`` by
# direct assignment, not a ``_TRANSITIONS`` edge, so ``is_terminal(ci_pending)``
# is wrongly True). Using an allowlist means a story mid-CI can never be
# mistaken for resolved — the fail-safe direction.
# ``blocked_ci_unresolved`` is the one BLOCKED_* state that IS resolved for
# issue-closing: the auto-merge worker only parks a story there AFTER closing its
# PR (CI-recovery exhausted, app-blocked). The work is terminally done for this
# attempt — the tracker issue should close, not linger. Every OTHER BLOCKED_*
# state stays absent (it may still be fixed and merged), preserving the fail-safe.
_RESOLVED_STORY_STATES = frozenset(
    {"deployed", "superseded_by_sibling", "closed", "blocked_ci_unresolved"}
)


def _direction_is_complete(rows: list[Any]) -> bool:
    """True when a direction's winner shipped and no child work is unresolved.

    Fixes the historical bug where the check required *every* story to be
    ``DEPLOYED``: a dual-draft direction can never satisfy that because its
    losing sibling lands in ``SUPERSEDED_BY_SIBLING``, so the tracker issue
    leaked open forever. We instead require (a) at least one DEPLOYED story
    (a real deliverable shipped) and (b) every child story in an explicitly
    *resolved* state (:data:`_RESOLVED_STORY_STATES`). Any other state —
    in-flight (``pr_open``/``ci_pending``/…) or ``BLOCKED_*`` — keeps the
    tracker open.
    """
    from factory.chain.state_machine import StoryState

    if not rows:
        return False
    if not any(r.state == StoryState.DEPLOYED.value for r in rows):
        return False  # nothing has shipped yet — keep the tracker open
    return all((r.state or "") in _RESOLVED_STORY_STATES for r in rows)


def maybe_close_tracker_issue(
    direction_id: str,
    app_config: AppConfig,
    github_client: Any,
    *,
    software_factory_root: Path,
    db_path: Path | None = None,
) -> bool:
    """Close a direction's tracker issue once the direction is complete.

    Reads the tracker number from the direction's ``state.yaml`` and checks the
    ``stories`` table via :func:`_direction_is_complete`: the tracker closes
    when at least one child story DEPLOYED and every child is resolved
    (deployed / superseded / invalidated), never while active or ``BLOCKED_*``
    work remains. Best-effort; returns True on close.
    """
    try:
        from sqlmodel import Session, select

        from factory.chain.state_machine import StoryRecord, StoryState
        from factory.directions.parser import parse_direction_dir
        from factory.runner import _engine

        root = Path(software_factory_root)
        # Locate the direction dir + its tracker issue number.
        dirs = list((root / "apps" / app_config.name / "directions").glob(f"{direction_id}-*"))
        if not dirs:
            return False
        direction = parse_direction_dir(app_config.name, dirs[0], software_factory_root=root)
        tracker = (direction.state or {}).get("tracker_issue")
        if not tracker:
            return False

        db = db_path or (root / "state" / "factory.db")
        with Session(_engine(db)) as session:
            rows = session.exec(
                select(StoryRecord).where(
                    StoryRecord.direction_id == direction_id,
                    StoryRecord.app == app_config.name,
                )
            ).all()
        if not _direction_is_complete(rows):
            return False  # still active (or blocked) work for this direction

        deployed = sum(1 for r in rows if r.state == StoryState.DEPLOYED.value)
        repo = github_client.get_repo(app_config.repo)
        issue = repo.get_issue(int(tracker))
        if str(getattr(issue, "state", "")).lower() == "closed":
            return False
        issue.create_comment(
            f"✅ Direction complete — {deployed} of {len(rows)} child stories deployed "
            "(remaining resolved/superseded). Closing the direction tracker."
        )
        issue.edit(state="closed")
        return True
    except Exception:  # noqa: BLE001
        return False


def reconcile_completed_issues(
    app_config: AppConfig,
    github_client: Any,
    *,
    software_factory_root: Path,
    db_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Backfill the GitHub-issue lifecycle: close issues left open for completed work.

    Idempotent + fail-safe (never raises). This is the "detect-and-remediate"
    safety net for issue-lifecycle drift — the event-driven close on deploy can
    no-op (e.g. a merge that lands via the async ``--auto`` path with no token
    in scope), leaving completed directions/stories with open trackers/issues.
    Running this reconciles that state and is safe to run repeatedly / on a
    schedule.

    Two passes, both scoped to ``app_config.name``:
      1. Direction trackers — close the tracker of every direction that is
         complete per :func:`_direction_is_complete` (winner deployed, no
         active/blocked child work).
      2. Story issues — close the issue of every story in a resolved-shipped
         state (``DEPLOYED`` or ``SUPERSEDED_BY_SIBLING``) whose issue is still
         open.

    An already-closed issue is never touched (state is checked first). Returns a
    report: ``{"trackers_closed", "stories_closed", "would_close", "errors"}``.
    """
    from collections import defaultdict

    report: dict[str, Any] = {
        "trackers_closed": [],
        "stories_closed": [],
        "would_close": [],
        "errors": [],
    }

    # Load story rows for this app. The DB read (missing / locked / corrupt
    # ``factory.db``) must NOT raise — this function's contract is fail-safe so
    # it can be run on a schedule / on the tick path without breaking it.
    try:
        from sqlmodel import Session, select

        from factory.chain.state_machine import StoryRecord, StoryState
        from factory.directions.parser import parse_direction_dir
        from factory.runner import _engine

        root = Path(software_factory_root)
        db = db_path or (root / "state" / "factory.db")
        with Session(_engine(db)) as session:
            story_rows = list(
                session.exec(select(StoryRecord).where(StoryRecord.app == app_config.name)).all()
            )
    except Exception as exc:  # noqa: BLE001 - a bad DB must not break the sweep
        report["errors"].append(("db", str(db_path or "state/factory.db"), str(exc)))
        return report

    by_direction: dict[str, list[Any]] = defaultdict(list)
    for r in story_rows:
        by_direction[r.direction_id].append(r)

    try:
        repo = github_client.get_repo(app_config.repo)
    except Exception as exc:  # noqa: BLE001 - a bad client must not raise
        report["errors"].append(("repo", app_config.repo, str(exc)))
        return report

    def _close_if_open(kind: str, number: Any, comment: str, key: str) -> bool:
        """Close one issue if currently open. Records the action in ``report``."""
        try:
            issue = repo.get_issue(int(number))
            if str(getattr(issue, "state", "")).lower() == "closed":
                return False
            if dry_run:
                report["would_close"].append((kind, int(number), key))
                return True
            issue.create_comment(comment)
            issue.edit(state="closed")
            return True
        except Exception as exc:  # noqa: BLE001 - one bad issue must not abort the sweep
            report["errors"].append((kind, number, str(exc)))
            return False

    # Pass 1 — direction trackers.
    directions_root = root / "apps" / app_config.name / "directions"
    for d in sorted(p for p in directions_root.glob("*") if p.is_dir()):
        direction_id = d.name.split("-", 1)[0]
        try:
            direction = parse_direction_dir(app_config.name, d, software_factory_root=root)
        except Exception:  # noqa: BLE001 - skip unparseable direction dirs
            continue
        tracker = (direction.state or {}).get("tracker_issue")
        if not tracker:
            continue
        rows = by_direction.get(direction_id, [])
        if not _direction_is_complete(rows):
            continue
        deployed = sum(1 for r in rows if r.state == StoryState.DEPLOYED.value)
        comment = (
            f"✅ Direction complete — {deployed} of {len(rows)} child stories deployed "
            "(remaining resolved/superseded). Closing the direction tracker (reconcile)."
        )
        if _close_if_open("tracker", tracker, comment, direction_id) and not dry_run:
            report["trackers_closed"].append((direction_id, int(tracker)))

    # Pass 2 — story issues for resolved-shipped stories.
    shipped = {StoryState.DEPLOYED.value, StoryState.SUPERSEDED_BY_SIBLING.value}
    for r in story_rows:
        num = getattr(r, "github_issue_number", None)
        if not num or r.state not in shipped:
            continue
        if r.state == StoryState.DEPLOYED.value:
            comment = "✅ Deployed — closing automatically (reconcile: story reached DEPLOYED)."
        else:
            comment = "🔁 Superseded by a sibling draft — closing automatically (reconcile)."
        if _close_if_open("story", num, comment, r.slug or str(r.id)) and not dry_run:
            report["stories_closed"].append((r.id, int(num)))

    return report
