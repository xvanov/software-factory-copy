"""Gate: ``tests-meaningful``.

Runs the slop detector against the PR's test diff. Zero findings → pass.
Any finding → fail with the findings as details.

If the app opts into mutation testing (``app_config.gates.mutation_testing
== True``), this gate ALSO would invoke mutmut/stryker — but Phase 4 just
wires the hook (sets a placeholder flag in details) without shipping a
working mutmut config. Apps opt-in later via their config.yaml.
"""

from __future__ import annotations

from factory.app_config import AppConfig
from factory.chain.gates.evaluator import GateResult, PRContext
from factory.chain.slop_detector import scan_diff


def evaluate(pr: PRContext, app_config: AppConfig) -> GateResult:
    label = "tests-meaningful"
    findings = scan_diff(pr.files_changed, repo_root=pr.repo_root)
    findings_dicts = [fnd.as_dict() for fnd in findings]
    if findings:
        return GateResult(
            label=label,
            passed=False,
            reason=f"{len(findings)} slop finding(s)",
            details={"findings": findings_dicts},
        )

    # Mutation hook (opt-in). Phase 4 only wires the flag — no mutmut
    # subprocess invocation yet because real-run requires app-specific
    # mutmut configuration the factory cannot synthesize.
    mutation_status = "skipped"
    if app_config.gates.mutation_testing:
        mutation_status = "opted_in_but_not_executed_in_phase_4"

    return GateResult(
        label=label,
        passed=True,
        reason="no slop findings",
        details={"mutation_status": mutation_status, "findings": []},
    )
