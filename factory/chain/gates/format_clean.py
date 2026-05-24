"""Gate: ``format-clean``. Runs ``format_check_command``.

Dry-run honors ``StoryRecord.format_passed``; ``None`` blocks as
``format_not_recorded`` so the gate cannot vacuously pass.
"""

from __future__ import annotations

from factory.app_config import AppConfig
from factory.chain.gates.evaluator import GateResult, PRContext, _run_command


def evaluate(pr: PRContext, app_config: AppConfig) -> GateResult:
    label = "format-clean"
    cmd = app_config.gates.format_check_command
    if not cmd:
        return GateResult(label=label, passed=True, reason="no format_check_command configured")
    if pr.dry_run:
        flag = getattr(pr.story, "format_passed", None) if pr.story is not None else None
        if flag is True:
            return GateResult(
                label=label,
                passed=True,
                reason="story.format_passed=True",
                details={"command": cmd},
            )
        if flag is False:
            return GateResult(
                label=label,
                passed=False,
                reason="story.format_passed=False",
                details={"command": cmd},
            )
        return GateResult(
            label=label,
            passed=False,
            reason="format_not_recorded",
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
