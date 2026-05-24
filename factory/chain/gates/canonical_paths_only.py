"""Gate: ``canonical-paths-only``.

Runs ``factory.context.enforcer.scan_pr_diff`` on the PR's file list.
Zero violations → pass.
"""

from __future__ import annotations

from factory.app_config import AppConfig
from factory.chain.gates.evaluator import GateResult, PRContext
from factory.context.enforcer import scan_pr_diff


def evaluate(pr: PRContext, app_config: AppConfig) -> GateResult:
    label = "canonical-paths-only"
    violations = scan_pr_diff(pr.files_changed)
    if violations:
        return GateResult(
            label=label,
            passed=False,
            reason=f"{len(violations)} canonical-paths violation(s)",
            details={"violations": [v._asdict() for v in violations]},
        )
    return GateResult(label=label, passed=True, reason="no violations")
