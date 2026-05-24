"""Gate evaluator + shared types.

This module centralizes the gate-running pipeline so handlers, the
auto-merge worker, and the CLI all reuse the same evaluation logic.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.app_config import AppConfig
from factory.chain.state_machine import StoryRecord

# The complete set of gate labels, in the order the chain expects them.
ALL_GATE_LABELS: list[str] = [
    "tests-red-first-confirmed",
    "tests-green",
    "tests-meaningful",
    "flow-verified",
    "coverage-verified",
    "lint-clean",
    "format-clean",
    "types-clean",
    "docs-current",
    "canonical-paths-only",
]


@dataclass
class PRContext:
    """Everything a gate needs about the PR under evaluation.

    Built by the auto-merge worker from GitHub + the local StoryRecord;
    handed to every gate evaluator. Gates do not call GH themselves
    (centralizes the API surface for testability + rate-limit budget).
    """

    pr_number: int
    head_sha: str
    base_branch: str
    files_changed: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    ci_state: str | None = None  # "success" | "failure" | "pending" | None
    repo_root: Path | None = None  # local checkout for real-run gate execution
    story: StoryRecord | None = None
    commit_history: list[dict[str, Any]] = field(default_factory=list)
    # ^ each entry: {"sha": str, "files": [str], "tests_run_red": bool|None}

    # The worker tells gates whether to actually shell out. dry_run=True
    # forces gates to read StoryRecord-recorded flags only.
    dry_run: bool = True


@dataclass
class GateResult:
    """The output of a single gate evaluation."""

    label: str
    passed: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "passed": self.passed,
            "reason": self.reason,
            "details": self.details,
        }


def gate_label_for(module_name: str) -> str:
    """Map ``tests_red_first_confirmed`` → ``tests-red-first-confirmed``."""
    return module_name.replace("_", "-")


def _run_command(cmd: str, cwd: Path | None) -> tuple[int, str]:
    """Run a shell command, return (exit_code, captured stderr/stdout).

    Centralized so gates have one place to swap for fakes in tests.
    """
    proc = subprocess.run(
        cmd,
        shell=True,  # noqa: S602 — gate commands come from trusted app config
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return proc.returncode, (proc.stdout + proc.stderr)[-4000:]


# --------------------------------------------------------------------------- #
# Aggregator
# --------------------------------------------------------------------------- #


def evaluate_all_gates(pr: PRContext, app_config: AppConfig) -> dict[str, GateResult]:
    """Run every gate; return ``{label: GateResult}`` mapping.

    Failure of one gate does not short-circuit the others — operators want
    to see every blocking issue at once, not play whack-a-mole.
    """
    from factory.chain.gates import (
        canonical_paths_only,
        coverage_verified,
        docs_current,
        flow_verified,
        format_clean,
        lint_clean,
        tests_green,
        tests_meaningful,
        tests_red_first_confirmed,
        types_clean,
    )

    out: dict[str, GateResult] = {}
    for mod in (
        tests_red_first_confirmed,
        tests_green,
        tests_meaningful,
        flow_verified,
        coverage_verified,
        lint_clean,
        format_clean,
        types_clean,
        docs_current,
        canonical_paths_only,
    ):
        result = mod.evaluate(pr, app_config)
        out[result.label] = result
    return out
