"""Gate: ``format-clean``. Runs ``format_check_command``."""

from __future__ import annotations

from factory.app_config import AppConfig
from factory.chain.gates.evaluator import GateResult, PRContext, _run_command


def evaluate(pr: PRContext, app_config: AppConfig) -> GateResult:
    label = "format-clean"
    cmd = app_config.gates.format_check_command
    if not cmd:
        return GateResult(label=label, passed=True, reason="no format_check_command configured")
    if pr.dry_run:
        return GateResult(
            label=label, passed=True, reason=f"dry-run; would run: {cmd}", details={"command": cmd}
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
