"""Gate: ``coverage-verified``.

Runs ``app_config.gates.coverage_command`` (which should embed
``--cov-fail-under=N`` or its equivalent for the app's language). The
exit code is the signal: 0 → coverage threshold met.

Dry-run path trusts a recorded flag on StoryRecord (currently sourced
from the test_implementer / dev handler results; absent for Phase 4
which only ships the gate plumbing — defaults to pass with a "skipped"
reason). When the chain handlers wire a ``coverage_exit_code`` field
in future phases, this gate reads it.
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
        return GateResult(
            label=label,
            passed=True,
            reason=f"dry-run; would run: {cmd}",
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
