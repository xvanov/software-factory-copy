"""Gate: ``docs-current``.

Asserts the Tech-Writer ran and recorded non-empty ``context_updates``,
OR explicitly marked "no updates needed" in the rationale.
"""

from __future__ import annotations

import json

from factory.app_config import AppConfig
from factory.chain.gates.evaluator import GateResult, PRContext


def evaluate(pr: PRContext, app_config: AppConfig) -> GateResult:
    label = "docs-current"
    story = pr.story
    if story is None:
        return GateResult(label=label, passed=False, reason="no story linked to PR")
    if not story.tech_writer_result_json:
        return GateResult(label=label, passed=False, reason="tech_writer never produced a result")
    try:
        tw = json.loads(story.tech_writer_result_json)
    except json.JSONDecodeError:
        return GateResult(label=label, passed=False, reason="tech_writer_result_json unparseable")

    updates = tw.get("context_updates") or []
    rationale = (tw.get("rationale") or "").lower()
    if updates:
        return GateResult(
            label=label,
            passed=True,
            reason=f"{len(updates)} context update(s)",
            details={"updates": updates},
        )
    # No updates: only acceptable if the writer explicitly justified it.
    no_updates_phrases = ("no updates needed", "no context updates", "no-op", "nothing to update")
    if any(p in rationale for p in no_updates_phrases):
        return GateResult(
            label=label,
            passed=True,
            reason="tech_writer marked 'no updates needed' with rationale",
            details={"rationale": rationale},
        )
    return GateResult(
        label=label,
        passed=False,
        reason="tech_writer produced 0 updates and no rationale for why",
        details={"rationale": rationale or "(empty)"},
    )
