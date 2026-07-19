"""Gate: ``acceptance-verified`` (WS1.2 — independent acceptance oracle).

The problem this closes
=======================

Loop-4 makes the dev author AND run its own tests. That is great for
convergence, but a coder that writes the tests judging it can reward-hack —
special-case the exact assertions, weaken them, or delete the hard ones
(ImpossibleBench: hiding the acceptance tests from the coder drops cheating to
~0). Every other merge gate re-derives truth from artifacts the dev produced
(``tests-green`` re-runs the dev's suite; ``tests-meaningful`` scans the dev's
tests). None of them is INDEPENDENT of the dev.

This gate is that independent layer. It runs an acceptance test authored from
the direction's acceptance criteria — the SPEC ONLY, blind to the dev's code
and the dev's tests — that the dev never sees or edits (authored at story spawn,
stored under ``state/acceptance/<app>/<story_id>/`` OUTSIDE the dev worktree;
see ``factory.chain.acceptance``). At merge time this gate copies that test into
the merge-candidate checkout, runs it against the app's python env, and passes
iff it exits 0 — so a story whose OWN tests are green but whose behaviour
violates an acceptance criterion is caught here.

The blocking decision keys off ``story.acceptance_expected`` — set at spawn to
"app opted in AND the direction has ACs", INDEPENDENT of whether authoring
succeeded — NOT off the presence of the stored file. That is the fix for the
false-green hole: an authoring failure leaves ``acceptance_expected=True`` with
no stored test, and this gate then BLOCKS AUTHORITATIVELY (never a silent skip).
It is not a permanent dead-end: ``acceptance.reauthor_missing_oracles`` re-authors
expected-but-missing oracles on a later tick (spec-only, so still dev-blind), so
the story heals and gets gated for real.

Resolution
==========

* Not opted in (``gates.acceptance_oracle`` False): PASS (skip). Never required.
* Opted in but this story is NOT expected to have an oracle (no ACs): PASS
  (skip, not applicable). ``required_gate_labels`` does not require it.
* Expected + stored test readable:
    - real-run (checkout present): copy in, run, pass iff exit 0, remove.
    - dry-run / no checkout: NON-AUTHORITATIVE (cannot verify), never a pass.
* Expected + stored test MISSING/unreadable (authoring flaked): AUTHORITATIVE
  BLOCK (passed=False, authoritative=True) — self-heal re-authors next tick.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from factory.app_config import AppConfig
from factory.chain.acceptance import ref_is_readable
from factory.chain.gates.evaluator import GateResult, PRContext, _run_command

_DEFAULT_ACCEPTANCE_COMMAND = "python -m pytest {test_file} -q"


def evaluate(pr: PRContext, app_config: AppConfig) -> GateResult:
    label = "acceptance-verified"
    gates = app_config.gates

    # Not opted in: skip (pass). Mirrors the optional command gates — a missing
    # capability means "this gate does not apply", not "this gate fails".
    if not gates.acceptance_oracle:
        return GateResult(
            label=label,
            passed=True,
            reason="acceptance oracle not enabled for this app (skipped)",
            details={"acceptance_oracle": False},
        )

    story = pr.story
    ref = getattr(story, "acceptance_test_ref", None) if story is not None else None
    # Expected is the required/blocking source of truth. Fall back to "a ref
    # exists" for legacy stories spawned before ``acceptance_expected`` existed.
    expected = bool(getattr(story, "acceptance_expected", False)) or bool(ref)

    if not expected:
        # Opted in, but this story has no acceptance criteria to verify — not
        # applicable, and required_gate_labels does not require it.
        return GateResult(
            label=label,
            passed=True,
            reason="story has no acceptance criteria (not applicable, skipped)",
            details={"acceptance_oracle": True, "acceptance_expected": False},
        )

    root = pr.software_factory_root

    # Expected but the stored oracle is missing/unreadable → authoring flaked.
    # BLOCK AUTHORITATIVELY (never a silent pass). The tick self-heal
    # (reauthor_missing_oracles) re-authors it before a later merge attempt, so
    # this is not a permanent dead-end.
    if not ref_is_readable(story, root):
        return GateResult(
            label=label,
            passed=False,
            reason=(
                "acceptance oracle EXPECTED but not available "
                f"(ref={ref!r}, root={'set' if root else 'unset'}) — authoring "
                "failed; blocking until it is re-authored (self-heals next tick)"
            ),
            details={"authoritative": True, "acceptance_expected": True,
                     "acceptance_test_ref": ref},
        )

    # Need a real checkout to run against. Dry-run (no worktree) cannot
    # re-derive truth — never claim a merge-authoritative pass.
    if pr.dry_run or pr.repo_root is None:
        return GateResult(
            label=label,
            passed=False,
            reason="[dry-run] acceptance oracle present but not run (no checkout)",
            details={"authoritative": False, "acceptance_test_ref": ref},
        )

    # Real-run: copy the independent test into the merge-candidate checkout,
    # run it against the app's env, then remove it (never leave it behind to be
    # committed). Named distinctively (story id, else head_sha) so it cannot
    # collide with app tests.
    # ref_is_readable(story, root) returned True above → story, ref, root all set.
    assert story is not None and ref is not None and root is not None
    sid = story.id if story.id is not None else pr.head_sha
    dest_name = f"test_acceptance_oracle_{sid}.py"
    dest = Path(pr.repo_root) / dest_name

    p = Path(ref)
    stored = p if p.is_absolute() else Path(root) / p

    cmd_template = gates.acceptance_test_command or _DEFAULT_ACCEPTANCE_COMMAND
    cmd = cmd_template.format(test_file=dest_name)
    try:
        shutil.copyfile(stored, dest)
        exit_code, output = _run_command(cmd, cwd=Path(pr.repo_root))
    finally:
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass

    return GateResult(
        label=label,
        passed=exit_code == 0,
        reason=f"ran independent acceptance oracle exit_code={exit_code}",
        details={
            "authoritative": True,
            "acceptance_test_ref": ref,
            "command": cmd,
            "output_tail": output,
        },
    )
