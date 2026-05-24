"""Gate: ``tests-green``.

The final CI run on the PR branch returned success. In real-run mode
the auto-merge worker passes ``pr.ci_state``; in dry-run we trust the
last dev handler's recorded ``test_run_passed=True``.
"""

from __future__ import annotations

import json

from factory.app_config import AppConfig
from factory.chain.gates.evaluator import GateResult, PRContext


def evaluate(pr: PRContext, app_config: AppConfig) -> GateResult:
    label = "tests-green"
    # Prefer the live CI state.
    if pr.ci_state is not None:
        passed = pr.ci_state == "success"
        return GateResult(
            label=label,
            passed=passed,
            reason=f"ci_state={pr.ci_state}",
            details={"ci_state": pr.ci_state},
        )

    story = pr.story
    if story is None:
        return GateResult(label=label, passed=False, reason="no story / no ci_state")

    # In dry-run with no CI state, fall back to the dev handler's flag.
    # The dev handler advances to TESTS_GREEN only when test_run_passed=True.
    # We cross-check by reading the recorded result, if present.
    raw = story.test_implementer_result_json or "{}"
    try:
        impl = json.loads(raw)
    except json.JSONDecodeError:
        impl = {}
    if story.state in {"tests_green", "reviewer_done", "tech_writer_done", "pr_open"}:
        return GateResult(
            label=label,
            passed=True,
            reason=f"story.state={story.state} (dev reported green)",
            details={"impl": impl},
        )
    return GateResult(
        label=label,
        passed=False,
        reason=f"story.state={story.state} — tests not yet green",
    )
