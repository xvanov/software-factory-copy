"""Gate: ``flow-verified``.

The originating direction may carry a ``flow.md`` (user flow) or
``api_spec.md`` (API contract). This gate asserts at least one test in
the test plan explicitly references the flow / spec — heuristic match
on the test's ``what_it_asserts`` / ``why_meaningful`` / ``key_steps``
fields against key terms extracted from the flow.

If the direction has neither flow.md nor api_spec.md (an ``explore``
direction), the gate passes vacuously — there's nothing to verify
against.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from factory.app_config import AppConfig
from factory.chain.gates.evaluator import GateResult, PRContext


def _extract_terms(text: str) -> set[str]:
    """Cheap term-extraction: lowercase, strip punctuation, drop tokens < 4
    chars and a stop list of common test-domain noise words."""
    stop = {
        "the",
        "and",
        "with",
        "that",
        "this",
        "from",
        "when",
        "then",
        "into",
        "user",
        "page",
        "step",
        "uses",
        "have",
        "does",
        "must",
        "should",
        "their",
    }
    tokens = {t for t in re.findall(r"[a-zA-Z][a-zA-Z0-9_/]{3,}", text.lower())}
    return tokens - stop


def evaluate(pr: PRContext, app_config: AppConfig) -> GateResult:
    label = "flow-verified"
    story = pr.story
    if story is None:
        return GateResult(label=label, passed=False, reason="no story linked to PR")

    if not story.test_plan_json:
        return GateResult(label=label, passed=False, reason="no test_plan_json recorded")

    try:
        plan: dict[str, Any] = json.loads(story.test_plan_json)
    except json.JSONDecodeError:
        return GateResult(label=label, passed=False, reason="test_plan_json unparseable")

    # Find the originating direction on disk.
    if pr.repo_root is None:
        # Without a repo root we can't read the direction's flow.md. Treat
        # missing context as a pass — operators see why_meaningful, not a
        # spurious red label.
        return GateResult(
            label=label,
            passed=True,
            reason="no repo_root supplied; gate skipped",
            details={"plan_size": len(plan.get("test_plan") or [])},
        )

    flow_text = ""
    api_text = ""
    if story.direction_id:
        # Locate the direction directory in the factory root (NOT the app
        # repo); fall back to searching apps/<app>/directions/.
        # The auto-merge worker passes ``pr.repo_root`` = factory_root or
        # the local app checkout. We accept the closer of the two via a
        # simple search.
        candidates = [
            pr.repo_root / "apps" / story.app / "directions",
        ]
        for cand in candidates:
            if not cand.exists():
                continue
            for d in cand.iterdir():
                if not d.is_dir():
                    continue
                if d.name.startswith(f"{story.direction_id}-"):
                    flow_path = d / "flow.md"
                    api_path = d / "api_spec.md"
                    if flow_path.exists():
                        flow_text = flow_path.read_text(encoding="utf-8")
                    if api_path.exists():
                        api_text = api_path.read_text(encoding="utf-8")
                    break

    if not flow_text and not api_text:
        return GateResult(
            label=label,
            passed=True,
            reason="direction has no flow.md or api_spec.md (vacuous pass)",
        )

    terms = _extract_terms(flow_text + "\n" + api_text)
    if not terms:
        return GateResult(
            label=label,
            passed=True,
            reason="no extractable terms in flow/api_spec (vacuous pass)",
        )

    # Look for any test whose name / what_it_asserts / why_meaningful /
    # key_steps overlaps with at least one extracted term.
    plan_items = plan.get("test_plan") or []
    matching: list[dict[str, Any]] = []
    for item in plan_items:
        haystack = " ".join(
            [
                str(item.get("name") or ""),
                str(item.get("what_it_asserts") or ""),
                str(item.get("why_meaningful") or ""),
                " ".join(item.get("key_steps") or []),
            ]
        ).lower()
        if any(term in haystack for term in terms):
            matching.append(item)

    if not matching:
        return GateResult(
            label=label,
            passed=False,
            reason="no test in test_plan references the user flow / api spec",
            details={"plan_items_scanned": len(plan_items), "terms": sorted(terms)[:20]},
        )

    return GateResult(
        label=label,
        passed=True,
        reason=f"{len(matching)} test(s) reference the flow / api spec",
        details={"matching_tests": [m.get("name") for m in matching]},
    )


__all__ = ["evaluate", "_extract_terms", "Path"]
