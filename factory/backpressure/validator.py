"""Structural validation on top of completeness.

``compute_completeness`` answers "does the direction have the right *kinds* of
artifacts?". ``validate_direction`` adds the next layer: are those artifacts
actually usable? A ``flow.md`` that's empty fails. An ``api_spec.md`` with no
endpoint line fails.

The chain consumes the ``ValidationResult`` for its pre-check. The PM persona
may still override if the structural check is overly strict for an edge case
(e.g. a single-line api_spec like ``DELETE /thing → 204``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from factory.backpressure.parser import (
    compute_completeness,
    has_meaningful_api_spec,
    has_meaningful_flow,
)
from factory.directions.parser import Direction

_HTTP_METHOD_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", re.IGNORECASE)
_PATH_RE = re.compile(r"(?:^|\s)/[A-Za-z0-9_\-./{}]+")


@dataclass
class ValidationResult:
    is_valid: bool
    missing: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    # Structural issues are NOT fatal: PM may still proceed but the chain
    # records them so the tracker issue can ask the user for richer
    # backpressure. ``severity`` summarises:
    #   * ``ok``       — everything passed (no missing, no structural issues)
    #   * ``warning``  — sufficient, but flow.md or api_spec.md is "thin"
    #   * ``blocking`` — direction is insufficient; PM cannot proceed
    structural_issues: list[str] = field(default_factory=list)
    severity: str = "ok"
    has_flow: bool = False
    has_api_spec: bool = False
    has_acceptance: bool = False
    explore_tag: bool = False


def _read_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _flow_md_useful(content: str) -> tuple[bool, str | None]:
    """True if flow.md has at least one numbered or bulleted step line."""
    body = "\n".join(ln for ln in content.splitlines() if not ln.strip().startswith("<!--"))
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    has_step = re.search(r"(?m)^\s*(?:\d+\.\s+\S|[-*]\s+\S)", body) is not None
    if not has_step:
        return False, "flow.md has no numbered or bulleted step lines"
    return True, None


def _api_spec_useful(content: str) -> tuple[bool, str | None]:
    """True if api_spec.md mentions an HTTP method AND a path."""
    body = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    if not _HTTP_METHOD_RE.search(body):
        return False, "api_spec.md has no HTTP method (GET/POST/...)"
    if not _PATH_RE.search(body):
        return False, "api_spec.md has no path (starts with /)"
    return True, None


def validate_direction(direction: Direction) -> ValidationResult:
    """Combine ``compute_completeness`` with structural file-content checks."""
    rep = compute_completeness(direction)
    issues: list[str] = []
    structural_issues: list[str] = []
    has_flow = rep.has_flow
    has_api_spec = rep.has_api_spec

    if rep.has_flow:
        ok, msg = _flow_md_useful(_read_or_empty(direction.dir_path / "flow.md"))
        if not ok:
            has_flow = False
            if msg:
                issues.append(msg)
        else:
            # Flow exists and parses as steps — but is it MEANINGFUL? A flow
            # of two unverbed bullets isn't enough for the Test-Designer to
            # build E2E coverage. Record as a structural issue, not a fatal.
            if not has_meaningful_flow(direction):
                structural_issues.append(
                    "flow.md has steps but is thin (fewer than 2 user-visible verbs)"
                )

    if rep.has_api_spec:
        ok, msg = _api_spec_useful(_read_or_empty(direction.dir_path / "api_spec.md"))
        if not ok:
            has_api_spec = False
            if msg:
                issues.append(msg)
        else:
            # Same logic for api_spec: method+path passed, but is there a
            # response code declared? Without one, Test-Designer can't
            # assert correctness.
            if not has_meaningful_api_spec(direction):
                structural_issues.append(
                    "api_spec.md has method+path but no response code (e.g. 200/400)"
                )

    is_sufficient = has_flow or has_api_spec or rep.explore_tag
    missing = list(rep.missing)
    if not is_sufficient:
        # Re-compute missing under the stricter check.
        missing = []
        if not has_flow:
            missing.append("user_flow")
        if not has_api_spec:
            missing.append("api_spec")
        if not rep.explore_tag:
            missing.append("explore_tag_or_artifacts")
        if not rep.has_acceptance:
            missing.append("acceptance_criteria")

    if not is_sufficient:
        severity = "blocking"
    elif structural_issues:
        severity = "warning"
    else:
        severity = "ok"

    return ValidationResult(
        is_valid=is_sufficient,
        missing=missing,
        issues=issues,
        structural_issues=structural_issues,
        severity=severity,
        has_flow=has_flow,
        has_api_spec=has_api_spec,
        has_acceptance=rep.has_acceptance,
        explore_tag=rep.explore_tag,
    )
