"""Per-direction backpressure completeness check.

This is the *fast pre-check* the chain runs before paying for an LLM call.
The PM persona's JSON output can override for ambiguous cases (e.g. the user
wrote a verbose flow.md but called it ``walkthrough.md`` — the persona may
still rule the direction sufficient). The structural rule encoded here is
the floor: if this report says ``is_sufficient=False``, downstream agents
will not produce useful work and the chain immediately routes back to the
user.

Rule: a direction has sufficient backpressure if and only if AT LEAST ONE of
``has_flow``, ``has_api_spec``, or ``explore_tag`` is true. Acceptance
criteria are tracked for the report but do NOT independently make a
direction sufficient (the user can ship "do the thing" with no AC if they
also say `(explore)`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from factory.directions.parser import Direction


@dataclass
class CompletenessReport:
    is_sufficient: bool
    missing: list[str] = field(default_factory=list)
    has_flow: bool = False
    has_api_spec: bool = False
    has_acceptance: bool = False
    explore_tag: bool = False


def compute_completeness(direction: Direction) -> CompletenessReport:
    """Return the structural completeness report for ``direction``."""
    has_flow = direction.has_flow
    has_api_spec = direction.has_api_spec
    explore_tag = direction.explore_tag
    has_acceptance = bool(direction.acceptance)

    is_sufficient = has_flow or has_api_spec or explore_tag

    missing: list[str] = []
    if not is_sufficient:
        if not has_flow:
            missing.append("user_flow")
        if not has_api_spec:
            missing.append("api_spec")
        if not explore_tag:
            missing.append("explore_tag_or_artifacts")
    if not has_acceptance:
        missing.append("acceptance_criteria")

    return CompletenessReport(
        is_sufficient=is_sufficient,
        missing=missing,
        has_flow=has_flow,
        has_api_spec=has_api_spec,
        has_acceptance=has_acceptance,
        explore_tag=explore_tag,
    )
