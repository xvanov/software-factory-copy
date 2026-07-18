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


def maybe_close_tracker_issue(
    direction_id: str,
    app_config: AppConfig,
    github_client: Any,
    *,
    software_factory_root: Path,
    db_path: Path | None = None,
) -> bool:
    """Close a direction's tracker issue once ALL its child stories deploy.

    Reads the tracker number from the direction's ``state.yaml`` and checks
    the ``stories`` table: if every story for ``direction_id`` is in state
    ``deployed``, closes the tracker. Best-effort; returns True on close.
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
        direction = parse_direction_dir(
            app_config.name, dirs[0], software_factory_root=root
        )
        tracker = (direction.state or {}).get("tracker_issue")
        if not tracker:
            return False

        db = db_path or (root / "state" / "factory.db")
        with Session(_engine(db)) as session:
            rows = session.exec(
                select(StoryRecord).where(StoryRecord.direction_id == direction_id)
            ).all()
        if not rows or any(r.state != StoryState.DEPLOYED.value for r in rows):
            return False  # still work in flight for this direction

        repo = github_client.get_repo(app_config.repo)
        issue = repo.get_issue(int(tracker))
        if str(getattr(issue, "state", "")).lower() == "closed":
            return False
        issue.create_comment(
            f"✅ All {len(rows)} child stories deployed — closing the direction tracker."
        )
        issue.edit(state="closed")
        return True
    except Exception:  # noqa: BLE001
        return False
