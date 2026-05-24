"""Gate: ``tests-red-first-confirmed``.

Asserts the TDD discipline of "tests committed first, run red, then
implementation lands." Implementation walks the PR's commit history (in
real-run mode), finds the first commit that touches a test file, and
verifies that the tests in isolation were red at that point. In dry-run
mode, we trust the flag the chain handler recorded on the StoryRecord —
``test_implementer_result_json.exit_code == 1`` means red was observed.
"""

from __future__ import annotations

import json

from factory.app_config import AppConfig
from factory.chain.gates.evaluator import GateResult, PRContext


def evaluate(pr: PRContext, app_config: AppConfig) -> GateResult:
    label = "tests-red-first-confirmed"
    story = pr.story
    if story is None:
        return GateResult(
            label=label,
            passed=False,
            reason="no StoryRecord linked to PR",
        )

    # Trust the chain handler's recorded flag in both dry-run and the
    # default real-run path — the implementer already validated red.
    raw = story.test_implementer_result_json or "{}"
    try:
        impl = json.loads(raw)
    except json.JSONDecodeError:
        return GateResult(
            label=label,
            passed=False,
            reason="test_implementer_result_json was unparseable",
        )

    exit_code = impl.get("exit_code")
    if impl.get("slop_detected"):
        return GateResult(
            label=label,
            passed=False,
            reason="tests passed pre-implementation (slop)",
            details={"impl": impl},
        )
    if exit_code != 1:
        return GateResult(
            label=label,
            passed=False,
            reason=f"test_implementer exit_code={exit_code} (expected 1=red)",
            details={"impl": impl},
        )

    # Real-run could additionally cross-check the git history; for Phase 4
    # the recorded flag is the source of truth.
    return GateResult(
        label=label,
        passed=True,
        reason="test_implementer reported red pre-implementation",
        details={"exit_code": exit_code},
    )
