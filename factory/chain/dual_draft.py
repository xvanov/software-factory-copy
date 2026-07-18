"""Phase 7 — dual-draft PR flow for ambiguous directions.

When a direction is ambiguous (PM ``confidence < 0.6`` OR ``(explore)``
tag in frontmatter) the factory spawns TWO StoryRecords with materially
different interpretations of the ask. Each story flows through the TDD
chain independently → two ``draft-alternative`` PRs land + a comparison
comment on the Direction Tracker linking both.

This module is intentionally narrow: the *decision* (`should_spawn_dual_draft`),
the *interpretation production* (`produce_interpretations`), and the
*tracker linkage* (`link_alternatives`). Spawning the StoryRecords stays
in ``handle_stories_spawned`` (the dual-draft path is a branch of the
same code path), so the chain's state-machine semantics don't bifurcate.

Dry-run friendly: ``produce_interpretations`` returns a deterministic
two-element list without invoking an LLM, so tests can drive the whole
flow offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.directions.parser import Direction

# Confidence threshold (inclusive on the "fire dual-draft" side).
# If pm_result["confidence"] < this value, the chain fires dual-draft.
CONFIDENCE_THRESHOLD = 0.6


@dataclass
class Interpretation:
    """One of (typically) two interpretations of an ambiguous direction.

    ``interpretation_id`` is the suffix the chain uses to disambiguate
    slugs / branches (``story/<n>-<slug>-alt-a``).
    """

    interpretation_id: str  # "alt-a" | "alt-b" | ...
    title: str
    body: str  # the rationale + scope for this interpretation
    key_assumption_diff: str  # one-sentence "what makes this differ"


def should_spawn_dual_draft(direction: Direction, pm_result: dict[str, Any]) -> bool:
    """Return True iff the chain should fire the dual-draft branch.

    Triggers:

      * ``direction.explore_tag`` is True (user explicitly tagged ``(explore)``)
      * ``pm_result["confidence"] < CONFIDENCE_THRESHOLD``

    Both can fire; the result is the same regardless of which triggered.
    """
    if direction.explore_tag:
        return True
    confidence = pm_result.get("confidence")
    try:
        return confidence is not None and float(confidence) < CONFIDENCE_THRESHOLD
    except (TypeError, ValueError):
        return False


def _dry_run_interpretations(
    direction: Direction, pm_result: dict[str, Any]
) -> list[Interpretation]:
    """Deterministic two-interpretation fallback used in dry-run.

    The factory has access to the direction title + acceptance criteria
    in dry-run; without an LLM, we synthesize two materially-different
    interpretations from the title using a stable convention:

      * **alt-a**: "narrow read" — minimal-scope interpretation
      * **alt-b**: "broad read" — wider-scope interpretation

    Each interpretation carries a ``key_assumption_diff`` operators can
    read in the comparison comment.
    """
    base_title = direction.title or direction.slug.replace("-", " ")
    why = (direction.why or "(no why provided)").strip().splitlines()
    why_first = why[0] if why else "(no why provided)"

    alt_a = Interpretation(
        interpretation_id="alt-a",
        title=f"{base_title} — narrow read",
        body=(
            f"**Interpretation A: minimal scope.**\n\n"
            f"Read of the user's intent: {why_first}\n\n"
            f"This interpretation assumes the smallest scope that satisfies the "
            f"explicit acceptance criteria. No incidental refactors. No "
            f"extending beyond what is stated."
        ),
        key_assumption_diff=(
            "Assumes the user wants the minimum viable change; declines to infer scope expansion."
        ),
    )
    alt_b = Interpretation(
        interpretation_id="alt-b",
        title=f"{base_title} — broad read",
        body=(
            f"**Interpretation B: broader scope.**\n\n"
            f"Read of the user's intent: {why_first}\n\n"
            f"This interpretation treats the acceptance criteria as a floor and "
            f"includes adjacent improvements (touched modules' rough edges, "
            f"obvious follow-on fixes the change makes natural)."
        ),
        key_assumption_diff=(
            "Assumes the user wants every adjacent rough edge touched while the file is open."
        ),
    )
    return [alt_a, alt_b]


def produce_interpretations(
    direction: Direction,
    pm_result: dict[str, Any],
    *,
    dry_run: bool = False,
    text_run: Any = None,
) -> list[Interpretation]:
    """Produce 2 interpretations of ``direction``.

    In dry-run, returns the deterministic ``_dry_run_interpretations``
    output. In real-run, this would call ``text_run("analyst", ...)``
    with a tight schema; we accept ``text_run`` as a parameter so a
    test can inject one without import-time wiring.
    """
    if dry_run or text_run is None:
        return _dry_run_interpretations(direction, pm_result)

    # Real-run: call the analyst persona for two structured interpretations.
    # We give it the direction body + acceptance + pm_result and ask for
    # a 2-element list with the same shape as ``Interpretation``.
    prompt = (
        f"Direction title: {direction.title}\n\n"
        f"Direction why: {direction.why or '(none)'}\n\n"
        f"Acceptance criteria:\n"
        + "\n".join(f"- {ac}" for ac in (direction.acceptance or ["(none provided)"]))
        + "\n\n"
        f"PM confidence: {pm_result.get('confidence', 'n/a')}\n\n"
        "The direction is ambiguous. Produce two structurally different "
        "interpretations of the user's intent. Output strict JSON matching "
        'the schema {"interpretations": [{"interpretation_id": str, "title": '
        'str, "body": str, "key_assumption_diff": str}, ...]}. Provide '
        "exactly 2 entries; their key_assumption_diff fields MUST contradict "
        "each other materially."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["interpretations"],
        "properties": {
            "interpretations": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "required": [
                        "interpretation_id",
                        "title",
                        "body",
                        "key_assumption_diff",
                    ],
                    "properties": {
                        "interpretation_id": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "key_assumption_diff": {"type": "string"},
                    },
                },
            },
        },
    }
    # Route the analyst persona through the model_router so the active
    # provider (Azure or direct) is honored at the call site rather than at
    # ``text_run`` resolution time. Imported lazily to keep this module's
    # import graph minimal in tests.
    from factory.model_router import route

    raw = text_run("analyst", prompt, model_id=route("analyst"), schema=schema)
    out: list[Interpretation] = []
    for entry in raw.get("interpretations", [])[:2]:
        out.append(
            Interpretation(
                interpretation_id=str(entry["interpretation_id"]),
                title=str(entry["title"])[:200],
                body=str(entry["body"]),
                key_assumption_diff=str(entry["key_assumption_diff"]),
            )
        )
    if len(out) != 2:
        # Real-run produced fewer than 2; fall back to deterministic.
        return _dry_run_interpretations(direction, pm_result)
    return out


# HTML-comment sentinel embedded in every link_alternatives comment so the
# function can detect its own prior posts and stay idempotent across reruns
# of ``handle_stories_spawned`` (e.g. retries, redeliveries).
LINK_ALTERNATIVES_SENTINEL = "<!-- factory:dual-draft-link -->"


def link_alternatives(
    story_a: Any,
    story_b: Any,
    interpretations: list[Interpretation],
    direction: Direction,
    github_client: Any,
    *,
    app_repo: str | None = None,
) -> int | None:
    """Post a comparison comment on the Direction Tracker linking both stories.

    Returns the comment id on a fresh post, ``None`` if no tracker issue is
    known, and the existing comment's id on a no-op idempotent rerun.

    Idempotency: every comment carries the
    ``<!-- factory:dual-draft-link -->`` sentinel. On reentry we scan
    existing comments for the sentinel and skip the post if one is
    present.
    """
    tracker = direction.state.get("tracker_issue") if direction.state else None
    if not isinstance(tracker, int) or tracker <= 0:
        return None
    if github_client is None:
        return None

    if app_repo is None:
        # Caller forgot to pass repo; nothing to do.
        return None

    repo = github_client.get_repo(app_repo)
    issue = repo.get_issue(tracker)

    # Idempotency check — if an existing comment already carries the
    # sentinel, return its id and skip the post. Best-effort: list-comments
    # may raise on a network blip; we treat that as "post anyway".
    try:
        existing = issue.get_comments()
    except Exception:  # pragma: no cover — defensive
        existing = []
    for comment in existing:
        body = getattr(comment, "body", "") or ""
        if LINK_ALTERNATIVES_SENTINEL in body:
            return int(getattr(comment, "id", 0)) or 1

    # Build the comparison comment body.
    int_a = interpretations[0] if len(interpretations) > 0 else None
    int_b = interpretations[1] if len(interpretations) > 1 else None

    lines: list[str] = []
    lines.append(LINK_ALTERNATIVES_SENTINEL)
    lines.append("## Dual-draft alternatives spawned")
    lines.append("")
    lines.append(
        "PM confidence below threshold or `(explore)` tag detected. The "
        "factory has spawned **two** child stories with materially-different "
        "interpretations. Each will produce its own draft PR labeled "
        "`draft-alternative`."
    )
    lines.append("")
    if int_a is not None:
        lines.append(f"### Story A (`{int_a.interpretation_id}`): {int_a.title}")
        lines.append(f"_{int_a.key_assumption_diff}_")
        if story_a is not None and getattr(story_a, "github_issue_number", None):
            lines.append(f"Tracker issue: #{story_a.github_issue_number}")
        lines.append("")
    if int_b is not None:
        lines.append(f"### Story B (`{int_b.interpretation_id}`): {int_b.title}")
        lines.append(f"_{int_b.key_assumption_diff}_")
        if story_b is not None and getattr(story_b, "github_issue_number", None):
            lines.append(f"Tracker issue: #{story_b.github_issue_number}")
        lines.append("")
    lines.append(
        "Close whichever PR's interpretation you prefer; the factory "
        "auto-cleans the other draft once one alternative merges."
    )

    comment = issue.create_comment("\n".join(lines))
    return int(getattr(comment, "id", 0)) or 1


# --------------------------------------------------------------------------- #
# Sibling cleanup — close the losing draft-alternative once one wins
# --------------------------------------------------------------------------- #

# ``handle_stories_spawned``'s dual-draft branch always suffixes each
# story's slug with its ``interpretation_id`` (``alt-a`` / ``alt-b`` / ...)
# so the two draft PRs don't collide on branch names. There's no dedicated
# DB column marking "this story is one of a dual-draft pair" — matching the
# slug suffix is how ``close_abandoned_draft_sibling`` (below) recognizes
# dual-draft siblings without a schema change.
_DRAFT_ALT_SLUG_RE = re.compile(r"-(alt-[a-z0-9]+)$")


def _draft_alt_suffix(slug: str) -> str | None:
    m = _DRAFT_ALT_SLUG_RE.search(slug or "")
    return m.group(1) if m else None


def close_abandoned_draft_sibling(
    winner: Any,
    app_config: Any,
    software_factory_root: Path,
    db_path: Path,
    github_client: Any,
    dry_run: bool,
) -> bool:
    """Close the losing dual-draft sibling's GitHub issue once ``winner`` merges.

    The dual-draft flow spawns two ``draft-alternative`` StoryRecords per
    ambiguous direction; the tracker comment (``link_alternatives``)
    promises "the factory auto-cleans the other draft once one alternative
    merges" but that cleanup never existed — whichever alternative's PR
    merged first left its sibling's issue (and branch) open forever (e.g.
    #210 orphaned after #209 merged — audit 2026-07-18, leak 4 of 4).

    ``winner`` is the StoryRecord whose PR just merged. Looks up sibling
    StoryRecords sharing ``direction_id`` + ``app``, filters to the ones
    that carry the dual-draft slug suffix (excluding the winner's own
    interpretation), and — for any still-open GitHub issue among them —
    posts an explanatory comment and closes it with reason "not planned".

    Best-effort and idempotent; never raises — a bookkeeping close must
    never break the merge worker. Returns True iff at least one sibling
    issue was closed.
    """
    if dry_run or github_client is None:
        return False
    if winner is None or not getattr(winner, "direction_id", None):
        return False
    winner_suffix = _draft_alt_suffix(getattr(winner, "slug", "") or "")
    if winner_suffix is None:
        return False  # not a dual-draft story; nothing to clean up

    try:
        from sqlmodel import Session, select

        from factory.chain.state_machine import StoryRecord
        from factory.runner import _engine

        eng = _engine(Path(db_path))
        with Session(eng) as session:
            siblings = session.exec(
                select(StoryRecord).where(
                    StoryRecord.direction_id == winner.direction_id,
                    StoryRecord.app == winner.app,
                )
            ).all()

        closed_any = False
        for sib in siblings:
            if sib.id == winner.id:
                continue
            sib_suffix = _draft_alt_suffix(sib.slug or "")
            if sib_suffix is None or sib_suffix == winner_suffix:
                # Not a dual-draft sibling, or the same interpretation
                # (shouldn't happen, but never self-close).
                continue
            if not sib.github_issue_number:
                continue
            try:
                repo = github_client.get_repo(app_config.repo)
                issue = repo.get_issue(int(sib.github_issue_number))
                if str(getattr(issue, "state", "")).lower() == "closed":
                    continue
                winner_ref = (
                    f"#{winner.github_issue_number}"
                    if getattr(winner, "github_issue_number", None)
                    else f"story {winner.id}"
                )
                issue.create_comment(
                    f"Superseded by sibling {winner_ref} which shipped — "
                    "closing this draft-alternative automatically."
                )
                issue.edit(state="closed", state_reason="not_planned")
                closed_any = True
            except Exception:  # noqa: BLE001 - bookkeeping must never break merge
                continue
        return closed_any
    except Exception:  # noqa: BLE001
        return False
