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
#
# The historical 11-label set carried six VESTIGIAL gates that read
# StoryRecord fields no Loop-4 handler ever writes, or payloads from personas
# deleted in the Loop-4 collapse (WS1.6, 2026-07-19):
#   * tests-red-first-confirmed / flow-verified — read the deleted
#     test_implementer / test_designer payloads.
#   * lint-clean / format-clean / types-clean / coverage-verified — read
#     StoryRecord.{lint,format,types,coverage}_passed flags that are always
#     None (nothing assigns them). None of the six were in the required set,
#     so they only ever produced non-blocking noise. Removed outright: a gate
#     that evaluates an unwritten flag is worse than no gate — it manufactures
#     a green/red signal detached from any real check.
ALL_GATE_LABELS: list[str] = [
    "tests-green",
    "tests-meaningful",
    "docs-current",
    "canonical-paths-only",
    "smoke-green",
]

# The labels REQUIRED to merge a Loop-4 (dev-owns-tests) story. These are the
# signals that still exist independently at merge time: the dev's recorded
# green run (re-derived by re-running the suite in real-run, WS1.4), the
# programmatic slop-gate veto on every real review, the reviewer's approval,
# and the docs-enforcer — all encoded in the story reaching a mergeable state.
LOOP4_REQUIRED_GATE_LABELS: list[str] = [
    "tests-green",
    "tests-meaningful",
    "docs-current",
    "canonical-paths-only",
]


def required_gate_labels(app_config: AppConfig) -> list[str]:
    """The merge-required gate labels for THIS app (D002).

    The Loop-4 base set is universal. Runtime gates are appended per-app, only
    when the app declares the capability — keeping the rollout opt-in so an app
    without a smoke harness sees no new merge blocks (the PRs 110/111 regression
    was caused by making a gate universally required before every app could
    satisfy it). ``smoke-green`` becomes required exactly when the app has a
    working, declared smoke harness.
    """
    labels = list(LOOP4_REQUIRED_GATE_LABELS)
    gates = app_config.gates
    if gates.smoke_harness_ready and gates.smoke_command:
        labels.append("smoke-green")
    return labels


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
    """Map ``canonical_paths_only`` → ``canonical-paths-only``."""
    return module_name.replace("_", "-")


def _run_command(cmd: str, cwd: Path | None) -> tuple[int, str]:
    """Run a shell command, return (exit_code, captured stderr/stdout).

    Centralized so gates have one place to swap for fakes in tests. A hung
    command must fail ITS gate, not abort the whole merge evaluation — the
    smoke gate boots a real stack and is the one command genuinely likely to
    hit the timeout, and evaluate_all_gates deliberately never short-circuits.
    """
    try:
        proc = subprocess.run(
            cmd,
            shell=True,  # noqa: S602 — gate commands come from trusted app config
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"", e.stderr or b"")
        tail = "".join(
            o.decode(errors="replace") if isinstance(o, bytes) else o for o in out
        )[-4000:]
        return 124, f"command timed out after 600s: {cmd}\n{tail}"
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
        docs_current,
        smoke_green,
        tests_green,
        tests_meaningful,
    )

    out: dict[str, GateResult] = {}
    for mod in (
        tests_green,
        tests_meaningful,
        docs_current,
        canonical_paths_only,
        smoke_green,
    ):
        result = mod.evaluate(pr, app_config)
        out[result.label] = result
    return out
