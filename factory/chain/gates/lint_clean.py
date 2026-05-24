"""Gate: ``lint-clean``.

Runs ``app_config.gates.lint_command`` in the app repo. Real-run shells
out; dry-run honors ``StoryRecord.lint_passed``: True/False round-trip as
pass/fail, ``None`` blocks the gate as ``lint_not_recorded`` so the dry
chain can't claim a clean lint signal it never observed.
"""

from __future__ import annotations

from factory.app_config import AppConfig
from factory.chain.gates.evaluator import GateResult, PRContext, _run_command


def evaluate(pr: PRContext, app_config: AppConfig) -> GateResult:
    label = "lint-clean"
    cmd = app_config.gates.lint_command
    if not cmd:
        return GateResult(label=label, passed=True, reason="no lint_command configured")
    if pr.dry_run:
        flag = getattr(pr.story, "lint_passed", None) if pr.story is not None else None
        if flag is True:
            return GateResult(
                label=label,
                passed=True,
                reason="story.lint_passed=True",
                details={"command": cmd},
            )
        if flag is False:
            return GateResult(
                label=label,
                passed=False,
                reason="story.lint_passed=False",
                details={"command": cmd},
            )
        return GateResult(
            label=label,
            passed=False,
            reason="lint_not_recorded",
            details={"command": cmd},
        )
    if pr.repo_root is None:
        return GateResult(label=label, passed=False, reason="no repo_root for real-run gate")
    exit_code, output = _run_command(cmd, cwd=pr.repo_root)
    return GateResult(
        label=label,
        passed=exit_code == 0,
        reason=f"exit_code={exit_code}",
        details={"command": cmd, "output_tail": output},
    )
