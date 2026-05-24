"""Gate: ``coverage-verified``.

Runs ``app_config.gates.coverage_command`` (which should embed
``--cov-fail-under=N`` or its equivalent for the app's language). The
exit code is the signal: 0 → coverage threshold met.

Dry-run honors ``StoryRecord.coverage_passed``; ``None`` blocks as
``coverage_not_recorded`` so the chain cannot claim a coverage signal
the dev/test handler never observed.
"""

from __future__ import annotations

from factory.app_config import AppConfig
from factory.chain.gates.evaluator import GateResult, PRContext, _run_command


def evaluate(pr: PRContext, app_config: AppConfig) -> GateResult:
    label = "coverage-verified"
    cmd = app_config.gates.coverage_command
    if not cmd:
        return GateResult(
            label=label,
            passed=True,
            reason="no coverage_command configured (vacuous pass)",
        )

    if pr.dry_run:
        flag = getattr(pr.story, "coverage_passed", None) if pr.story is not None else None
        if flag is True:
            return GateResult(
                label=label,
                passed=True,
                reason="story.coverage_passed=True",
                details={"command": cmd},
            )
        if flag is False:
            return GateResult(
                label=label,
                passed=False,
                reason="story.coverage_passed=False",
                details={"command": cmd},
            )
        return GateResult(
            label=label,
            passed=False,
            reason="coverage_not_recorded",
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
