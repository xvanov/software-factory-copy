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

from dataclasses import dataclass
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

    Returns the comment id (truthy on success) or ``None`` if no
    tracker issue is known. Idempotent only in the trivial sense: it
    will append a new comment every call. (The Direction Tracker carries
    an updated body via ``open_or_update_tracker_issue`` separately.)
    """
    tracker = direction.state.get("tracker_issue") if direction.state else None
    if not isinstance(tracker, int) or tracker <= 0:
        return None
    if github_client is None:
        return None

    if app_repo is None:
        # Caller forgot to pass repo; nothing to do.
        return None

    # Build the comparison comment body.
    int_a = interpretations[0] if len(interpretations) > 0 else None
    int_b = interpretations[1] if len(interpretations) > 1 else None

    lines: list[str] = []
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

    repo = github_client.get_repo(app_repo)
    issue = repo.get_issue(tracker)
    comment = issue.create_comment("\n".join(lines))
    return int(getattr(comment, "id", 0)) or 1
