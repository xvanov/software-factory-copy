"""Gate: ``tests-green``.

The story's test suite passes. This gate is merge-authoritative, so at merge
time it must RE-DERIVE that truth rather than trust a flag some earlier tick
recorded (WS1.4, 2026-07-19). The false-green class this closes: a story
reaches ``pr_open`` with ``test_run_passed`` set on some historical attempt,
the branch then drifts (rebase, sibling merge, a "fix" that broke the suite),
and the recorded flag still reads green — so the merge worker trusts a run
that no longer reflects the code being merged.

Resolution order:

* REAL-RUN (``dry_run=False`` and a story worktree is checked out): re-run the
  app's ``gates.test_command`` in that worktree; pass IFF it exits 0. This is
  the authoritative signal. When no ``test_command`` is configured, fall back
  to the live CI state (the next-best real signal); if neither exists we
  cannot re-derive and the gate blocks.
* DRY-RUN (no worktree — tests, CLI, planning ticks): assert the recorded
  CI/state signal, but the reason is explicitly tagged ``[dry-run]`` so it is
  never mistaken for a merge-authoritative pass.
"""

from __future__ import annotations

from factory.app_config import AppConfig
from factory.chain.gates.evaluator import GateResult, PRContext, _run_command

_GREEN_STATES = {"tests_green", "reviewer_done", "tech_writer_done", "pr_open"}


def evaluate(pr: PRContext, app_config: AppConfig) -> GateResult:
    label = "tests-green"

    # ------------------------------------------------------------------ #
    # REAL-RUN: re-derive truth against the actual checked-out code.
    # ------------------------------------------------------------------ #
    if not pr.dry_run and pr.repo_root is not None:
        cmd = app_config.gates.test_command
        if cmd:
            exit_code, output = _run_command(cmd, cwd=pr.repo_root)
            return GateResult(
                label=label,
                passed=exit_code == 0,
                reason=f"re-ran test_command exit_code={exit_code}",
                details={"command": cmd, "output_tail": output, "authoritative": True},
            )
        # No test_command to re-run — fall back to the live CI verdict, which
        # is still a real run of the branch (just not one we drove here).
        if pr.ci_state is not None:
            return GateResult(
                label=label,
                passed=pr.ci_state == "success",
                reason=f"no test_command configured; live ci_state={pr.ci_state}",
                details={"ci_state": pr.ci_state, "authoritative": True},
            )
        return GateResult(
            label=label,
            passed=False,
            reason="real-run but no test_command and no ci_state — cannot re-derive tests-green",
            details={"authoritative": True},
        )

    # ------------------------------------------------------------------ #
    # DRY-RUN: no checkout to run against. Assert recorded signals only,
    # and never claim a merge-authoritative pass.
    # ------------------------------------------------------------------ #
    if pr.ci_state is not None:
        return GateResult(
            label=label,
            passed=pr.ci_state == "success",
            reason=f"[dry-run] ci_state={pr.ci_state}",
            details={"ci_state": pr.ci_state, "authoritative": False},
        )

    story = pr.story
    if story is None:
        return GateResult(label=label, passed=False, reason="no story / no ci_state")

    if story.state in _GREEN_STATES:
        return GateResult(
            label=label,
            passed=True,
            reason=f"[dry-run] story.state={story.state} (dev reported green; not re-run)",
            details={"authoritative": False},
        )
    return GateResult(
        label=label,
        passed=False,
        reason=f"story.state={story.state} — tests not yet green",
    )
