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
    # mutmut configuration the factory cannot synthesize. P5.0 MEDIUM-3
    # carry-over: when an app opts in but no runner is wired the gate
    # must FAIL rather than silently pass; otherwise mutation_testing=true
    # is a no-op that gives operators false confidence.
    if app_config.gates.mutation_testing:
        return GateResult(
            label=label,
            passed=False,
            reason="mutation_testing opted-in but no runner wired",
            details={"mutation_status": "opted_in_no_runner", "findings": []},
        )

    return GateResult(
        label=label,
        passed=True,
        reason="no slop findings",
        details={"mutation_status": "skipped", "findings": []},
    )
