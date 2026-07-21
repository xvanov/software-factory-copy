"""Auto-merge worker — merges PRs that pass all required gates.

Polls every open PR for an app, evaluates the gate set, and squash-merges
when:

  * every gate label is present on the PR (the chain handlers add them
    as gates pass on previous ticks)
  * no blocking label (``do-not-merge`` / ``needs-human-verification`` /
    ``needs-direction`` / ``tests-slop`` / ``needs-test-quality-fix``) is
    present
  * the StoryRecord linked to the PR is in a state where merge is
    expected (``pr_open``, ``ci_green``, ``ready_for_merge``)

Records every merge attempt in ``state/factory.db.merge_actions`` for
the rollback worker to consult.

Dry-run is truly dry: GH is not contacted at all; the worker takes a
``fixture_prs`` list so tests can exercise the decision-and-record path
without network.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Field, Session, SQLModel, create_engine, select

from factory.app_config import (
    FACTORY_REPO,  # noqa: F401 - re-exported for callers/tests
    AppConfig,
    load_app_config,
    targets_factory_repo,
)
from factory.chain.gates.evaluator import (  # noqa: F401 - ALL_GATE_LABELS re-exported
    ALL_GATE_LABELS,
    LOOP4_REQUIRED_GATE_LABELS,
    PRContext,
    evaluate_all_gates,
    required_gate_labels,
)
from factory.chain.state_machine import StoryRecord, StoryState

# Labels that, when present, BLOCK merge regardless of gate status.
BLOCKING_LABELS: frozenset[str] = frozenset(
    {
        "do-not-merge",
        "needs-human-verification",
        "needs-direction",
        "tests-slop",
        "needs-test-quality-fix",
    }
)

# Story states from which the worker will consider merging.
_MERGEABLE_STATES = {
    StoryState.PR_OPEN.value,
    StoryState.CI_GREEN.value,
    StoryState.READY_FOR_MERGE.value,
}

# CI-failure -> dev re-fix loop (the operator's "run the real CI, check the
# output, and if it craps out, fix what crapped out" loop). Bounds how many
# times ``_handle_ci_failure`` will re-dispatch the SAME story back to dev
# before giving up and leaving it for a human — mirrors
# ``orchestrator._MAX_AUTO_RECOVERIES``'s cap + signature-guard pattern so a
# CI failure the dev cannot fix escalates instead of looping forever.
_MAX_CI_FIX_CYCLES = 3

# Gate labels the docs chain produces. The docs chain skips the TDD
# red→green loop, so the 10 TDD gate labels don't apply. The single
# label the docs chain DOES enforce is ``canonical-paths-only`` — when
# ``handle_docs_enforcer`` reaches PR_OPEN with no violations, the
# canonical-paths gate has effectively passed for the PR.
#
# Kept as a frozenset so the worker can swap the gate set on
# ``chain_kind`` without re-checking the TDD list.
_DOCS_CHAIN_GATE_LABELS: frozenset[str] = frozenset({"canonical-paths-only"})

def _story_targets_factory_repo(app_config: AppConfig) -> bool:
    """True when ``app_config`` builds the factory's own repo (a self-edit app).

    Scoping guard for the chain-side staging gate: only factory-repo stories are
    ever routed through staging. Every other app (sacrifice, ...) targets a
    different repo and bypasses the gate entirely.
    """
    return targets_factory_repo(app_config.repo)


class MergeActionRecord(SQLModel, table=True):
    """One row per auto-merge decision (merged or no-op)."""

    __tablename__ = "merge_actions"

    id: int | None = Field(default=None, primary_key=True)
    app: str = Field(index=True)
    pr_number: int = Field(index=True)
    head_sha: str
    merged: bool
    reason: str
    gates_passed_json: str  # JSON list of labels that passed
    blocking_labels_json: str  # JSON list of blocking labels present
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class MergeAction:
    """Returned to the caller — pure data shape for the CLI/tests."""

    app: str
    pr_number: int
    merged: bool
    reason: str
    gates_passed: list[str] = field(default_factory=list)
    blocking_labels: list[str] = field(default_factory=list)
    # Set True when a factory self-edit was refused by the chain-side staging
    # gate (``_evaluate_self_edit_gate``): the live factory was NOT touched and
    # the story should be sunk to a blocked/attention state by the caller.
    staging_blocked: bool = False
    # The staging-gate status when blocked: ``"staging_rejected"`` (a stage
    # failed on the clone), ``"staging_infra_failed"`` (harness could not
    # determine health), ``"forbidden"`` (touched factory/manager/** or
    # bench/**), or ``"diff_unavailable"`` (could not fetch the diff to
    # validate). ``None`` when the merge was not staging-blocked.
    staging_status: str | None = None
    # Set True when ``gh pr merge --auto`` succeeded but the PR is NOT merged
    # yet — GitHub's auto-merge was merely ENABLED and the merge will happen
    # asynchronously once the required checks pass. This is the critical
    # ``merged != auto-merge-requested`` distinction: with ``merged=False`` the
    # caller must NOT record a merged row, NOT enqueue a deploy, and NOT advance
    # the story — it STAYS in ``_MERGEABLE_STATES`` so ``reconcile_from_github``
    # and the CI-failure->dev loop keep watching it. If a required check later
    # FAILS, the PR never merges and the story is still re-dispatchable instead
    # of being stranded at ``deploy_pending``.
    auto_merge_enabled: bool = False


@dataclass
class FixturePR:
    """A fixture PR shape for dry-run tests.

    Mirrors the subset of GH fields the worker reads. ``story`` may be
    omitted if the fixture wants the worker to look up the StoryRecord by
    pr_number from the local DB.
    """

    pr_number: int
    head_sha: str
    base_branch: str
    labels: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    ci_state: str | None = "success"
    story: StoryRecord | None = None
    repo_root: Path | None = None


def _engine(db_path: Path) -> Any:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(f"sqlite:///{db_path}", echo=False)
    SQLModel.metadata.create_all(eng)
    return eng


def _record_merge_action(action: MergeAction, head_sha: str, db_path: Path) -> None:
    eng = _engine(db_path)
    rec = MergeActionRecord(
        app=action.app,
        pr_number=action.pr_number,
        head_sha=head_sha,
        merged=action.merged,
        reason=action.reason,
        gates_passed_json=json.dumps(action.gates_passed),
        blocking_labels_json=json.dumps(action.blocking_labels),
    )
    with Session(eng) as session:
        session.add(rec)
        session.commit()


def _is_docs_chain(story: StoryRecord | None) -> bool:
    """Return True if ``story`` is part of the docs chain.

    A missing story is treated as TDD (the historical default) so the
    full 10-gate check still applies — this keeps the worker
    conservative when the fixture skips the StoryRecord lookup.
    """
    return story is not None and story.chain_kind == "docs"


# ---------------------------------------------------------------------------
# Chain-side staging gate for factory self-edits (Tier 3 — self-tick safety)
# ---------------------------------------------------------------------------


@dataclass
class _SelfEditDecision:
    """Outcome of the chain-side staging gate for one factory-repo story.

    ``allow=True`` means the change is safe to merge (either it is not a factory
    self-edit, or the staging clone validated it healthy). Every other outcome
    is ``allow=False`` — the live factory is NEVER touched on uncertainty.
    """

    allow: bool
    status: str
    logs_tail: str = ""
    forbidden: bool = False


def _default_patch_provider(
    app_config: AppConfig, pr_number: int
) -> str | None:  # pragma: no cover - real-run gh shell-out
    """Fetch a PR's unified diff via ``gh pr diff``. ``None`` on any failure.

    A ``None`` return is treated as fail-safe by the caller (a factory self-edit
    whose diff cannot be read is never merged).
    """
    import subprocess

    if pr_number <= 0:
        return None
    try:
        proc = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--repo", app_config.repo],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


def _escalate_self_edit(
    escalate: Any,
    *,
    story: StoryRecord | None,
    app_config: AppConfig,
    root: Path,
    pr_number: int,
    classification: str,
    detail: str,
    patch: str = "",
) -> None:
    """Best-effort escalation for a refused factory self-edit. Never raises.

    Reuses the WS3.1 ``escalation.notify_escalation`` channel (the same one the
    manager proposal path uses) so a chain-built self-edit that fails staging or
    hits a forbidden path is surfaced to a human exactly like a manager-proposed
    one.
    """
    proposal = {
        "proposal_id": (
            f"chain-selfedit-story-{getattr(story, 'id', None)}-pr-{pr_number}"
        ),
        "concern_title": f"chain factory self-edit PR #{pr_number}",
        "proposal": {"suggested_patch": patch},
        "detail": detail,
    }
    try:
        escalate(
            proposal,
            root=root,
            repo=app_config.repo,
            classification=classification,
            result={"detail": detail, "pr_number": pr_number},
        )
    except Exception:  # noqa: BLE001 - escalation is best-effort; never block the tick
        pass


def _evaluate_self_edit_gate(
    *,
    app_config: AppConfig,
    story: StoryRecord | None,
    pr_number: int,
    root: Path | None,
    patch_provider: Any = None,
    self_edit_gate: Any = None,
    escalate: Any = None,
) -> _SelfEditDecision:
    """Decide whether a factory-repo story is safe to merge.

    This is the chain analogue of the manager proposal path's staging gate: a
    story that modifies the factory's OWN code must be validated by ACTUALLY
    RUNNING a cloned factory (``staging.gate_self_edit``) before it can land on
    the live factory. The manager path had this protection; the chain
    (pm-sync → dev → review → auto_merge) did not — this closes that gap.

    Fail-safe contract (never merge an unvalidated factory self-edit):
      * app does not target the factory repo → ``allow=True`` (app-repo stories
        bypass staging entirely — unchanged path).
      * diff cannot be obtained → ``allow=False`` (cannot validate → refuse).
      * touches a forbidden path (``factory/manager/**`` or ``bench/**``) →
        ``allow=False`` (the chain can never edit the safety mechanism or the
        grader, staging-validated or not).
      * not a runtime self-edit (e.g. only ``apps/factory/directions`` docs) →
        ``allow=True`` (staging validates "does the factory run"; a non-code
        change can't change that, so no staging is required).
      * runtime self-edit → routed through the staging gate; ``allow`` mirrors
        ``decision.promote`` (healthy → merge, unhealthy/infra → refuse).

    Any uncertainty — a missing diff, a staging harness exception, a
    non-promote decision — resolves to ``allow=False``.
    """
    if not _story_targets_factory_repo(app_config):
        return _SelfEditDecision(allow=True, status="not_factory_repo")

    root = Path(root) if root is not None else Path.cwd()

    from factory.chain.factory_improver_apply import _diff_target_paths
    from factory.manager import staging
    from factory.manager.apply import _any_path_is_forbidden_in_patch

    if patch_provider is None:
        patch_provider = _default_patch_provider
    if self_edit_gate is None:
        self_edit_gate = staging.gate_self_edit
    if escalate is None:
        from factory.manager.escalation import notify_escalation as escalate

    try:
        patch = patch_provider(app_config, pr_number)
    except Exception:  # noqa: BLE001 - fail-safe: cannot read diff → do not merge
        patch = None

    if not patch or not patch.strip():
        reason = (
            f"factory self-edit PR #{pr_number}: could not obtain the diff to "
            f"validate; refusing to merge an unvalidated factory change."
        )
        _escalate_self_edit(
            escalate,
            story=story,
            app_config=app_config,
            root=root,
            pr_number=pr_number,
            classification="escalate_to_human",
            detail=reason,
        )
        return _SelfEditDecision(allow=False, status="diff_unavailable", logs_tail=reason)

    paths = _diff_target_paths(patch)

    # Fail-safe: a NON-empty diff that parses to NO target paths is
    # unparseable. For a factory-repo story we cannot determine what it
    # touches, so we cannot rule out a self-edit or a forbidden path — refuse
    # rather than fall through to the "not a self-edit → merge" branch below
    # (which would be a fail-OPEN on an unreadable factory diff).
    if not paths:
        reason = (
            f"factory self-edit PR #{pr_number}: diff is non-empty but no target "
            f"paths could be parsed; refusing to merge a factory change whose "
            f"scope cannot be determined."
        )
        _escalate_self_edit(
            escalate,
            story=story,
            app_config=app_config,
            root=root,
            pr_number=pr_number,
            classification="escalate_to_human",
            detail=reason,
            patch=patch,
        )
        return _SelfEditDecision(allow=False, status="unparseable_diff", logs_tail=reason)

    # Forbidden-path guard FIRST — the chain must never edit the safety
    # mechanism (factory/manager/**) or the grader (bench/**), regardless of
    # whether staging would pass. Reuses the exact classifier the manager
    # apply path uses so the two paths can never diverge.
    if _any_path_is_forbidden_in_patch(paths, patch):
        reason = (
            f"factory self-edit PR #{pr_number} touches a forbidden path "
            f"(factory/manager/** or bench/**); the chain may not edit the "
            f"safety mechanism or the grader. Refusing to merge."
        )
        _escalate_self_edit(
            escalate,
            story=story,
            app_config=app_config,
            root=root,
            pr_number=pr_number,
            classification="forbidden",
            detail=reason,
            patch=patch,
        )
        return _SelfEditDecision(
            allow=False, status="forbidden", forbidden=True, logs_tail=reason
        )

    # Not a runtime self-edit (touches no factory/ code) → nothing for staging
    # to validate; safe to merge like any other content/docs change.
    if not staging.is_self_edit(paths):
        return _SelfEditDecision(allow=True, status="not_self_edit")

    # Runtime self-edit → validate by actually running the cloned factory.
    proposal = {
        "proposal_id": f"chain-selfedit-story-{getattr(story, 'id', None)}-pr-{pr_number}",
        "concern_title": (
            getattr(story, "title", None) or f"chain factory self-edit PR #{pr_number}"
        ),
        "proposal": {"suggested_patch": patch},
    }
    proposal_path = f"chain:{app_config.repo}:pr-{pr_number}"
    try:
        decision = self_edit_gate(proposal, proposal_path, root=root)
    except Exception as exc:  # noqa: BLE001 - fail-safe: harness error → do not merge
        reason = f"factory self-edit staging harness errored: {exc!r}"
        _escalate_self_edit(
            escalate,
            story=story,
            app_config=app_config,
            root=root,
            pr_number=pr_number,
            classification="escalate_to_human",
            detail=reason,
            patch=patch,
        )
        return _SelfEditDecision(
            allow=False, status="staging_infra_failed", logs_tail=reason
        )

    if getattr(decision, "promote", False):
        return _SelfEditDecision(allow=True, status="staging_validated")

    # Not promoted (unhealthy validation or infra failure). gate_self_edit
    # already emitted its own alert/event; add the chain escalation so the
    # blocked story is visible on the same channel as manager escalations.
    status = getattr(decision, "status", "staging_rejected") or "staging_rejected"
    logs_tail = getattr(decision, "logs_tail", "") or ""
    _escalate_self_edit(
        escalate,
        story=story,
        app_config=app_config,
        root=root,
        pr_number=pr_number,
        classification="escalate_to_human",
        detail=f"factory self-edit failed staging ({status}): {logs_tail[:500]}",
        patch=patch,
    )
    return _SelfEditDecision(allow=False, status=status, logs_tail=logs_tail)


def _evaluate_one_pr(
    *,
    app: str,
    fixture: FixturePR,
    app_config: AppConfig,
    dry_run: bool,
    github_client: Any,
    merge_method: str = "squash",
    wait_for_ci: bool = True,
    delete_branch_after_merge: bool = True,
    software_factory_root: Path | None = None,
    self_edit_gate: Any = None,
    patch_provider: Any = None,
    escalate: Any = None,
    merge_fn: Any = None,
    pr_merged_query: Any = None,
) -> MergeAction:
    """Decide if a PR should be merged; merge it in real-run.

    Branches on ``story.chain_kind``:

    * ``tdd`` (or unknown): the historical 10-gate check.
    * ``docs``: the docs chain skips the TDD red→green loop and
      doesn't apply the 10 TDD labels; the canonical-paths enforcer
      runs before reaching PR_OPEN, so we only check
      mergeable-state + blocking-labels.

    ``merge_fn`` / ``pr_merged_query`` are injection seams for the real-run
    merge shell-out (``_gh_pr_merge``) and the authoritative
    "is-this-PR-actually-merged" query (``_pr_is_merged_on_github``). Tests
    pass fakes to drive the "auto-merge enabled but not merged yet" vs "really
    merged" branches without touching GitHub.
    """
    story = fixture.story
    docs_chain = _is_docs_chain(story)
    merge_fn = merge_fn or _gh_pr_merge
    pr_merged_query = pr_merged_query or _pr_is_merged_on_github

    # Real-run short-circuit: if the PR is ALREADY merged on GitHub, we are
    # done — record the merge and let the caller enqueue the deploy + advance
    # the story. This cheaply handles the "auto-merge completed between ticks"
    # case (the async merge that ``--auto`` requested on a prior tick has since
    # landed) AND avoids re-running the expensive staging gate every tick while
    # a merge is pending. Fail-safe: an unconfirmed state returns False, so we
    # fall through to the normal gated evaluation. Dry-run never touches GH.
    if not dry_run and fixture.pr_number > 0:
        try:
            already_merged = bool(
                pr_merged_query(
                    app_config=app_config,
                    pr_number=fixture.pr_number,
                    github_client=github_client,
                )
            )
        except Exception:  # noqa: BLE001 - fail-safe: cannot confirm → not merged
            already_merged = False
        if already_merged:
            return MergeAction(
                app=app,
                pr_number=fixture.pr_number,
                merged=True,
                reason="already merged on GitHub",
            )

    # The TDD gate evaluator is only relevant for the TDD chain; for
    # docs PRs we skip it (the enforcer already vetted the diff in the
    # ``handle_docs_enforcer`` step).
    if docs_chain:
        gates_passed: list[str] = sorted(_DOCS_CHAIN_GATE_LABELS)
        missing_labels: list[str] = []
    else:
        pr_ctx = PRContext(
            pr_number=fixture.pr_number,
            head_sha=fixture.head_sha,
            base_branch=fixture.base_branch,
            files_changed=fixture.files_changed,
            labels=list(fixture.labels),
            ci_state=fixture.ci_state,
            repo_root=fixture.repo_root,
            software_factory_root=software_factory_root,
            story=story,
            dry_run=dry_run,
        )
        results = evaluate_all_gates(pr_ctx, app_config)
        gates_passed = [label for label, r in results.items() if r.passed]

        # Compute the labels the chain would have added on previous
        # ticks. In real-run we trust the actual PR labels; in dry-run
        # we synthesize from the gate results so a test fixture with
        # all gates green is "all labels present" without having to
        # enumerate them in the fixture.
        # A gate is satisfied by an applied PR label OR by the evaluation
        # that just ran — in real-run too. Nothing applies gate labels
        # under Loop-4 (the labelling stages died with the test-first
        # machinery), and a fresh evaluator pass is strictly stronger
        # evidence than a label applied on some earlier tick anyway.
        present_labels = set(fixture.labels) | set(gates_passed)
        missing_labels = [
            label
            for label in required_gate_labels(app_config, story)
            if label not in present_labels
        ]

    blocking_present = sorted(set(fixture.labels) & BLOCKING_LABELS)

    # Story state guard (applies to both chains).
    if story is not None and story.state not in _MERGEABLE_STATES:
        return MergeAction(
            app=app,
            pr_number=fixture.pr_number,
            merged=False,
            reason=f"story.state={story.state} not in mergeable states",
            gates_passed=gates_passed,
            blocking_labels=blocking_present,
        )

    if blocking_present:
        return MergeAction(
            app=app,
            pr_number=fixture.pr_number,
            merged=False,
            reason=f"blocking labels present: {blocking_present!r}",
            gates_passed=gates_passed,
            blocking_labels=blocking_present,
        )

    if missing_labels:
        return MergeAction(
            app=app,
            pr_number=fixture.pr_number,
            merged=False,
            reason=f"missing gate labels: {missing_labels!r}",
            gates_passed=gates_passed,
            blocking_labels=blocking_present,
        )

    # Synthesized placeholder (no real PR exists — ``pr_no = -(story.id)``
    # in the fixture-synthesis path). NEVER shell it into ``gh pr merge``:
    # a negative number parses as a flag ("unknown shorthand flag: '5' in
    # -56", observed live 2026-07-18) and the failure terminally blocked the
    # story. Record an auditable no-PR decision instead; the story needs PR
    # (re-)creation, not a merge.
    if fixture.pr_number <= 0:
        return MergeAction(
            app=app,
            pr_number=fixture.pr_number,
            merged=False,
            reason=(
                "no real PR exists for this story (placeholder number); "
                "needs PR creation via docs_enforcer/_open_pr_for_story"
            ),
            gates_passed=gates_passed,
            blocking_labels=blocking_present,
        )

    # Chain-side staging gate for factory self-edits. A story that modifies the
    # factory's OWN code must be validated by running a cloned factory before it
    # can land on the live factory — the same protection the manager proposal
    # path has (WS3.5). App-repo stories (sacrifice, ...) short-circuit to
    # allow=True inside the helper, so this is a no-op for them. Skipped in
    # dry-run UNLESS a gate is injected (tests): a dry-run merges nothing, so
    # there is nothing to protect, and we avoid the network/clone cost.
    if (not dry_run) or (self_edit_gate is not None):
        se_decision = _evaluate_self_edit_gate(
            app_config=app_config,
            story=story,
            pr_number=fixture.pr_number,
            root=software_factory_root,
            patch_provider=patch_provider,
            self_edit_gate=self_edit_gate,
            escalate=escalate,
        )
        if not se_decision.allow:
            return MergeAction(
                app=app,
                pr_number=fixture.pr_number,
                merged=False,
                reason=(
                    f"factory self-edit refused by chain-side staging gate "
                    f"({se_decision.status}); live factory not touched"
                ),
                gates_passed=gates_passed,
                blocking_labels=blocking_present,
                staging_blocked=True,
                staging_status=se_decision.status,
            )

    reason = (
        "docs chain enforcer passed; no blocking labels"
        if docs_chain
        else "all required gates passed; no blocking labels"
    )

    # Dry-run merges nothing; the decision path still returns merged=True so
    # tests exercise the record/deploy plumbing without touching GitHub.
    if dry_run:
        return MergeAction(
            app=app,
            pr_number=fixture.pr_number,
            merged=True,
            reason=reason,
            gates_passed=gates_passed,
            blocking_labels=blocking_present,
        )

    # Gates passed + no blockers. Merge.
    merge_err = merge_fn(
        app_config=app_config,
        pr_number=fixture.pr_number,
        merge_method=merge_method,
        wait_for_ci=wait_for_ci,
        delete_branch=delete_branch_after_merge,
        github_client=github_client,
    )
    if merge_err is not None:
        return MergeAction(
            app=app,
            pr_number=fixture.pr_number,
            merged=False,
            reason=f"gh merge failed: {merge_err}",
            gates_passed=gates_passed,
            blocking_labels=blocking_present,
        )

    # CRITICAL: ``merged`` must reflect a REAL GitHub merge, not merely that a
    # merge was requested. With ``wait_for_ci=False`` ``gh pr merge`` (no
    # ``--auto``) merges SYNCHRONOUSLY, so success == merged. With
    # ``wait_for_ci=True`` the shell-out used ``--auto``, which only ENABLES
    # auto-merge and returns 0 immediately WITHOUT merging when required checks
    # are still pending — so we must QUERY GitHub's authoritative state and only
    # claim a merge if the PR actually merged now (e.g. checks were already
    # green so ``--auto`` merged immediately). If it is NOT merged yet, return a
    # NON-merged action flagged ``auto_merge_enabled`` so the caller leaves the
    # story in ``_MERGEABLE_STATES`` (reconcile + the CI-failure loop keep
    # watching it) instead of stranding it at ``deploy_pending``.
    if wait_for_ci:
        try:
            really_merged = bool(
                pr_merged_query(
                    app_config=app_config,
                    pr_number=fixture.pr_number,
                    github_client=github_client,
                )
            )
        except Exception:  # noqa: BLE001 - fail-safe: cannot confirm → not merged
            really_merged = False
        if not really_merged:
            return MergeAction(
                app=app,
                pr_number=fixture.pr_number,
                merged=False,
                reason="auto-merge enabled; awaiting required checks",
                gates_passed=gates_passed,
                blocking_labels=blocking_present,
                auto_merge_enabled=True,
            )

    return MergeAction(
        app=app,
        pr_number=fixture.pr_number,
        merged=True,
        reason=reason,
        gates_passed=gates_passed,
        blocking_labels=blocking_present,
    )


def _gh_pr_merge(
    *,
    app_config: AppConfig,
    pr_number: int,
    merge_method: str,
    wait_for_ci: bool,
    delete_branch: bool,
    github_client: Any,
) -> str | None:  # pragma: no cover - real-run path; no tests exercise gh shell-out
    """Invoke ``gh pr merge`` for the PR. Returns None on success or an
    error string on failure.

    Uses the ``gh`` CLI (rather than pygithub) so ``--auto`` is
    available — pygithub's ``pr.merge()`` cannot wait for required
    checks. The shell-out is fenced behind ``pragma: no cover`` because
    no test exercises this real-run path.
    """
    import subprocess

    method_flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}.get(
        merge_method, "--squash"
    )
    cmd = ["gh", "pr", "merge", str(pr_number), "--repo", app_config.repo, method_flag]
    if wait_for_ci:
        cmd.append("--auto")
    if delete_branch:
        cmd.append("--delete-branch")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        # ``--auto`` ENABLES auto-merge, which GitHub refuses when nothing
        # blocks the merge: "clean status" (no required checks pending) or
        # "Protected branch rules not configured". The PR is immediately
        # mergeable — merge it directly instead of reporting failure
        # (PR 111, 2026-06-11; also the May-29/30 docs-chain merge errors).
        if wait_for_ci and (
            "clean status" in stderr
            or "Protected branch rules not configured" in stderr
        ):
            direct_cmd = [c for c in cmd if c != "--auto"]
            try:
                subprocess.run(direct_cmd, check=True, capture_output=True, text=True)
                return None
            except subprocess.CalledProcessError as exc2:
                return f"gh exit={exc2.returncode}: {(exc2.stderr or '').strip()}"
        return f"gh exit={exc.returncode}: {stderr}"
    except FileNotFoundError:
        # gh not installed — fall back to pygithub if available.
        if github_client is None:
            return "gh CLI not found and no github_client provided"
        try:
            repo = github_client.get_repo(app_config.repo)
            pr = repo.get_pull(pr_number)
            pr.merge(merge_method=merge_method)
        except Exception as exc:
            return f"pygithub merge failed: {exc!r}"
    return None


def _pr_is_merged_on_github(
    *, app_config: AppConfig, pr_number: int, github_client: Any = None
) -> bool:  # pragma: no cover - real-run path; queries live GH state
    """Return True iff the PR is ACTUALLY merged on GitHub right now.

    ``gh pr merge --auto`` returns exit 0 the instant it ENABLES auto-merge —
    it does NOT wait for or perform the merge. So a successful ``_gh_pr_merge``
    is NOT evidence the PR merged; only GitHub's authoritative state is. This
    queries ``gh pr view <n> --json state,mergedAt`` and treats the PR as merged
    iff ``mergedAt`` is non-null (equivalently ``state == "MERGED"``).

    Fail-safe: ANY failure (gh missing, timeout, non-zero exit, unparseable
    payload, non-positive placeholder PR) returns ``False``. We NEVER claim a
    merge we cannot positively confirm, so a story is never advanced to
    ``deploy_pending`` on an unconfirmed merge.
    """
    import json as _json
    import subprocess

    if pr_number <= 0:
        return False
    cmd = [
        "gh", "pr", "view", str(pr_number), "--repo", app_config.repo,
        "--json", "state,mergedAt",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    try:
        data = _json.loads(proc.stdout)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    merged_at = data.get("mergedAt")
    state = str(data.get("state", "")).upper()
    return bool(merged_at) or state == "MERGED"


def _pr_terminally_unmergeable(
    *, app_config: AppConfig, pr_number: int, github_client: Any
) -> bool:  # pragma: no cover - real-run path; queries live GH state
    """Return True when a PR can never be merged by retrying.

    A merge can fail transiently (CI still pending, ``--auto`` not yet
    enabled). But a PR that is CLOSED, already MERGED out-of-band, or
    CONFLICTING/DIRTY will fail on *every* tick — retrying it forever wedges
    the auto-merge worker and keeps drive_chain idle-spinning on a story that
    can never advance. Detect those terminal conditions so the caller can sink
    the story to a blocked state instead.
    """
    import json as _json
    import subprocess

    cmd = [
        "gh", "pr", "view", str(pr_number), "--repo", app_config.repo,
        "--json", "state,mergeable,mergeStateStatus",
    ]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
        data = _json.loads(out)
    except subprocess.CalledProcessError:
        # gh couldn't resolve the PR at all (deleted / wrong repo) — that is
        # itself terminal: retrying will never succeed.
        return True
    except (FileNotFoundError, ValueError):
        # gh missing or unparseable output — don't make a terminal call we
        # can't justify; let the normal retry path continue.
        return False
    state = str(data.get("state", "")).upper()
    mergeable = str(data.get("mergeable", "")).upper()
    merge_status = str(data.get("mergeStateStatus", "")).upper()
    return state in ("CLOSED", "MERGED") or mergeable == "CONFLICTING" or merge_status == "DIRTY"


def _query_ci_state(*, app_config: AppConfig, pr_number: int) -> str | None:
    """Query the REAL CI conclusion for a PR via the ``gh`` CLI.

    Returns ``"success"`` (every REQUIRED check passed/skipped), ``"failure"``
    (at least one required check failed/cancelled/errored), ``"pending"``
    (required checks still queued/running), or ``None`` when there is nothing
    to gate on — no branch protection / no required checks, ``gh``
    missing/timeout, a placeholder (non-positive) PR number, or unparseable
    output. ``None`` makes the ``tests-green`` gate fall back to the recorded
    dev flag, so apps without CI/protection (e.g. sacrifice pre-CI) are
    unaffected while protected repos gate on their real Actions run.

    This replaces the historical hardcoded ``ci_state="success"``, which let
    the gate pass on a string literal instead of a real signal (the "thinks
    CI passed then it crashes" class). Read-only; safe in dry-run.

    Implementation note: ``gh pr checks`` (v2.45) does NOT support ``--json``,
    so we parse its tab-separated rows and use ``--required`` so non-required
    integrations (e.g. CodeRabbit, which can sit PENDING forever) never poison
    the aggregate and block the factory's own merges. CRITICAL: gh prints
    "no required checks reported" and exits 0 when protection is absent —
    that must map to ``None`` (nothing to consult), never ``"success"``.
    """
    import subprocess

    if pr_number <= 0:  # synthesized placeholder (dry-run docs) — nothing to query
        return None
    cmd = [
        "gh", "pr", "checks", str(pr_number), "--repo", app_config.repo, "--required",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    combined = ((proc.stdout or "") + " " + (proc.stderr or "")).lower()
    if "no required checks" in combined or "no checks reported" in combined:
        # No protection / no required checks → nothing real to gate on.
        return None
    # Each row: "<name>\t<status>\t<elapsed>\t<url>"; status is the 2nd column.
    statuses: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].strip():
            statuses.add(parts[1].strip().lower())
    if not statuses:
        return None
    if statuses & {"fail", "failing", "failure", "cancel", "cancelled", "timed_out", "error"}:
        return "failure"
    if statuses & {"pending", "in_progress", "queued", "waiting"}:
        return "pending"
    # Remaining rows are all pass/skipping/neutral → required set is green.
    return "success"


def _ci_failure_signature(log_text: str) -> str:
    """Signature of a real-CI failure digest.

    Normalized the SAME way ``orchestrator._story_failure_signature``
    normalizes dev/review failure text (timestamps/paths/durations/addresses
    stripped) — reusing that normalization (rather than maintaining a second
    copy of the regex list) is what lets ``_handle_ci_failure`` tell "the dev
    fixed something and CI failed for a NEW reason" apart from "CI failed for
    the exact same reason again", the same distinction
    ``_recover_blocked_stories`` makes for blocked-state recoveries.

    Returns ``""`` when there is no log text (e.g. the best-effort log fetch
    came back empty) — an empty signature never matches a prior one, so a
    missing-evidence case never falsely looks like "no new signal".
    """
    from factory.chain.orchestrator import _normalize_failure_text

    text = (log_text or "").strip()
    if not text:
        return ""
    normalized = _normalize_failure_text(text)
    return hashlib.sha256(normalized[-500:].encode("utf-8")).hexdigest()


def _fetch_ci_failure_logs(*, app_config: AppConfig, pr_number: int) -> str:
    """Best-effort fetch of the failing CI run's log digest for ``pr_number``.

    Finds the PR's most recent failed Actions run via ``gh pr view --json
    headRefName,statusCheckRollup`` — preferring a failed check's
    ``detailsUrl`` (points straight at its run) and falling back to ``gh run
    list --branch <headRefName>`` when no ``detailsUrl`` resolves to a run id
    — then pulls the failing job's log lines via ``gh run view --log-failed``.

    Returns the last ~4000 chars of the digest, or ``""`` on ANY error,
    timeout, or empty result. This feeds a dev prompt, not a merge gate — a
    fetch failure must never crash the auto-merge tick.
    """
    import re as _re
    import subprocess

    if pr_number <= 0:  # synthesized placeholder — nothing real to look up
        return ""
    try:
        view = subprocess.run(
            [
                "gh", "pr", "view", str(pr_number), "--repo", app_config.repo,
                "--json", "headRefName,statusCheckRollup",
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if view.returncode != 0:
        return ""
    try:
        data = json.loads(view.stdout or "{}")
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""

    head_ref = str(data.get("headRefName") or "")
    run_id: str | None = None
    rollup = data.get("statusCheckRollup") or []
    if isinstance(rollup, list):
        for check in rollup:
            if not isinstance(check, dict):
                continue
            conclusion = str(check.get("conclusion") or "").upper()
            if conclusion in {"FAILURE", "CANCELLED", "TIMED_OUT", "ERROR"}:
                url = str(check.get("detailsUrl") or "")
                m = _re.search(r"/actions/runs/(\d+)", url)
                if m:
                    run_id = m.group(1)
                    break

    if run_id is None and head_ref:
        try:
            listed = subprocess.run(
                [
                    "gh", "run", "list", "--repo", app_config.repo,
                    "--branch", head_ref, "--limit", "5",
                    "--json", "databaseId,conclusion,status",
                ],
                capture_output=True, text=True, timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""
        if listed.returncode == 0:
            try:
                runs = json.loads(listed.stdout or "[]")
            except ValueError:
                runs = []
            if isinstance(runs, list):
                for run in runs:
                    if (
                        isinstance(run, dict)
                        and str(run.get("conclusion") or "").lower() == "failure"
                    ):
                        run_id = str(run.get("databaseId"))
                        break

    if not run_id:
        return ""

    try:
        log_proc = subprocess.run(
            ["gh", "run", "view", run_id, "--repo", app_config.repo, "--log-failed"],
            capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    digest = (log_proc.stdout or "").strip() or (log_proc.stderr or "").strip()
    if not digest:
        return ""
    return digest[-4000:]


def _handle_ci_failure(
    *,
    story: StoryRecord,
    app_config: AppConfig,
    pr_number: int,
    db: Path,
    root: Path,
) -> bool:
    """Feed a real CI failure back to dev instead of just skipping the merge.

    This closes the loop the operator asked for: "run the real CI, check the
    output, and if it craps out, fix what crapped out, then move on." Before
    this, ``_query_ci_state`` returning ``"failure"`` only made the merge
    gate decline to merge — nothing told the dev WHAT failed.

    Called when a mergeable-state story's real ``ci_state`` is ``"failure"``.
    Returns ``True`` if the story was re-dispatched back to dev (the caller
    should skip merging it this tick); ``False`` if the story was left alone
    (not mergeable) or escalated instead (cap reached / identical failure).

    Bounded two ways so this can never become a new infinite loop, mirroring
    ``orchestrator._recover_blocked_stories``:

      * a hard cap (``_MAX_CI_FIX_CYCLES``) on prior ``ci_fix_redispatch``
        events for THIS story;
      * a signature guard — if the current CI failure digest hashes
        identically to the one recorded at the story's most recent
        ``ci_fix_redispatch``, the dev's last attempt didn't actually fix it,
        so we stop instead of burning another dev cycle on the same dead end.

    Either bound trips a deduped ``ci_fix_exhausted`` event (an operator/FMS
    signal) and returns ``False``. Otherwise it re-dispatches the story back
    to dev via the EXISTING reviewer-findings plumbing (``handle_dev`` already
    reads ``story.reviewer_result_json`` into a findings list and feeds it to
    the sandbox — see ``handlers._handle_dev_once``) and returns ``True``.
    """
    from factory.chain.event_log import log_story_event, read_story_events
    from factory.chain.handlers import persist_story

    if story.state not in _MERGEABLE_STATES:
        # Defensive: callers already guard on this, but never re-dispatch a
        # story that isn't actually sitting in a mergeable state.
        return False

    events = read_story_events(story.id, software_factory_root=root, slug_hint=story.slug)
    prior_redispatches = [e for e in events if e.get("event") == "ci_fix_redispatch"]
    already_escalated = any(e.get("event") == "ci_fix_exhausted" for e in events)

    def _escalate(reason: str) -> None:
        if already_escalated:
            return
        try:
            log_story_event(
                story.id,
                "ci_fix_exhausted",
                {
                    "pr_number": pr_number,
                    "redispatches": len(prior_redispatches),
                    "cap": _MAX_CI_FIX_CYCLES,
                    "reason": reason,
                },
                software_factory_root=root,
                slug_hint=story.slug,
            )
        except Exception:  # noqa: BLE001
            pass

    if len(prior_redispatches) >= _MAX_CI_FIX_CYCLES:
        _escalate("cap_reached")
        return False

    try:
        logs = _fetch_ci_failure_logs(app_config=app_config, pr_number=pr_number)
    except Exception:  # noqa: BLE001
        logs = ""
    signature = _ci_failure_signature(logs)

    if prior_redispatches:
        last_signature = prior_redispatches[-1].get("failure_signature")
        if signature and last_signature is not None and signature == last_signature:
            # The last redispatch's CI failure recurred verbatim — the dev
            # didn't actually fix it. Recovering again would just grind
            # through another full dev cycle for no new signal.
            _escalate("identical_failure_signature")
            return False

    digest = logs.strip() or (
        "(no CI log digest could be fetched; inspect the GitHub Actions run "
        f"for PR #{pr_number} directly)"
    )
    # Emit a well-formed finding DICT (not a bare string): every downstream
    # consumer — runner._build_initial_message, _findings_signature,
    # _append_reviewer_history, _render_reviewer_history_section — indexes
    # findings with ``f.get(...)``. A string element crashed the dev
    # re-dispatch ("'str' object has no attribute 'get'"), silently breaking
    # this entire CI-failure feedback loop.
    finding = {
        "severity": "high",
        "criterion": "ci",
        "location": f"GitHub Actions CI (PR #{pr_number})",
        "what": (
            f"Real GitHub Actions CI failed on PR #{pr_number}. Fix the exact "
            f"failure it reported below — do not just re-run or ignore it:\n\n{digest}"
        ),
    }
    reviewer_payload = {
        "findings": [finding],
        "source": "ci_failure",
        "summary": "Real GitHub Actions CI failed; fix the exact failure it reported.",
    }

    # Re-entry point: REVIEWER_REQUESTED_CHANGES (not DEV_IN_PROGRESS) is the
    # correct target — ``DEV_IN_PROGRESS`` has no entry in the orchestrator's
    # per-state dispatch table (it's the transient "handler is actively
    # running" state that ``handle_dev`` itself transitions into and out of
    # within a single invocation; see ``orchestrator._DISPATCH``). Setting
    # the state directly to DEV_IN_PROGRESS would strand the story with no
    # handler ever picking it up again. REVIEWER_REQUESTED_CHANGES dispatches
    # to "dev" on the next tick AND is the exact existing path that feeds
    # ``story.reviewer_result_json`` into ``handle_dev``'s reviewer_findings
    # (see ``handlers._handle_dev_once``) — precisely the plumbing this
    # re-dispatch needs to reuse.
    story.reviewer_result_json = json.dumps(reviewer_payload)
    story.state = StoryState.REVIEWER_REQUESTED_CHANGES.value
    # Reset BOTH counters (mirror _recover_blocked_stories). A CI-fix redispatch
    # hits an already-approved story whose reviewer_cycles may be near
    # _MAX_REVIEW_CYCLES; without resetting it, the follow-up review pass could
    # trip BLOCKED_REVIEW_NONCONVERGENT on the first non-approving pass and
    # mislabel a CI-only hiccup as review non-convergence.
    story.dev_retries = 0
    story.reviewer_cycles = 0
    persist_story(story, db)

    try:
        log_story_event(
            story.id,
            "ci_fix_redispatch",
            {
                "pr_number": pr_number,
                "attempt": len(prior_redispatches) + 1,
                "cap": _MAX_CI_FIX_CYCLES,
                "failure_signature": signature,
            },
            software_factory_root=root,
            slug_hint=story.slug,
        )
    except Exception:  # noqa: BLE001
        pass
    return True


def _attempt_pr_reconcile(*, app_config: AppConfig, pr_number: int) -> bool:
    """Try to make a stale PR mergeable by merging the base branch into it.

    Uses ``gh pr update-branch`` — a MERGE of the base into the PR head (never a
    force-push / history rewrite), so it's safe to run automatically on the
    factory's own story branches. This fixes the common auto-merge failure where
    the PR fell BEHIND a moved base (e.g. two docs PRs touching adjacent context
    files); it canNOT resolve true content conflicts (those return non-zero and
    fall through to the terminal-block path for regeneration/human handling).

    Returns True if the update command succeeded (branch advanced).
    """
    import subprocess  # pragma: no cover - real-run path

    try:  # pragma: no cover - real-run path
        subprocess.run(
            ["gh", "pr", "update-branch", str(pr_number), "--repo", app_config.repo],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return True
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):  # pragma: no cover - real-run path
        return False


def auto_merge_tick(
    software_factory_root: Path,
    app: str,
    *,
    dry_run: bool = True,
    fixture_prs: list[FixturePR] | None = None,
    github_client: Any = None,
    db_path: Path | None = None,
    merge_method: str = "squash",
    wait_for_ci: bool = True,
    delete_branch_after_merge: bool = True,
    self_edit_gate: Any = None,
    patch_provider: Any = None,
    escalate: Any = None,
    merge_fn: Any = None,
    pr_merged_query: Any = None,
) -> list[MergeAction]:
    """Single pass of the auto-merge worker against ``app``.

    ``self_edit_gate`` / ``patch_provider`` / ``escalate`` are injection seams
    for the chain-side factory self-edit staging gate (``_evaluate_self_edit_gate``);
    they default to the real ``staging.gate_self_edit`` / ``gh pr diff`` /
    ``escalation.notify_escalation`` implementations. Tests pass fakes to drive
    the healthy / unhealthy / infra-failure / forbidden branches without cloning
    the factory or touching GitHub.

    Returns a ``MergeAction`` per PR evaluated. Tests pass
    ``fixture_prs`` to drive the decision logic without GitHub.

    In real-run with ``github_client`` set, the worker enumerates open
    PRs via the GH API and converts them to ``FixturePR`` records. (The
    enumeration code path lives behind a ``# pragma: no cover`` guard —
    full integration tests need a live GH; out of scope for Phase 4.)
    """
    root = Path(software_factory_root)
    db = db_path or (root / "state" / "factory.db")
    cfg = load_app_config(app, root)

    fixtures: list[FixturePR] = list(fixture_prs or [])
    def _story_worktree(root: Path, app: str, db_story: StoryRecord | None) -> Path | None:
        """The story's chain worktree, RECREATED on demand — command gates
        (smoke-green boots the PR's OWN code) need a local tree to run in.
        Worktrees are keyed by GITHUB ISSUE NUMBER, not the db row id (see
        handlers._writing_worktree). Existence alone isn't enough: the
        pruner reaps worktrees of waiting-state stories, which left PR 226
        permanently unmergeable on 'missing smoke-green' (2026-07-18) —
        ensure_worktree_for_story is idempotent and checks the story's
        branch back out from origin when the tree is gone."""
        if db_story is None:
            return None
        try:
            import subprocess

            from factory.app_config import resolve_app_repo_path
            from factory.chain.worktree import ensure_worktree_for_story

            source_repo = resolve_app_repo_path(cfg, root)
            wt = ensure_worktree_for_story(
                source_repo,
                software_factory_root=root,
                app=app,
                story_id=db_story.github_issue_number,
                slug=db_story.slug,
                base_branch=cfg.default_branch or "main",
            )
            # Fetch-before-trust: the gate must evaluate the EXACT commit that
            # will be squash-merged — origin/<feature_branch> — not whatever
            # local ref the worktree reuse path happened to leave checked out.
            # gh pr update-branch (_attempt_pr_reconcile) writes a merge commit
            # straight to origin, and other worktrees/rebases can advance the
            # remote tip, leaving the local feature ref behind; without this
            # sync the smoke/tests gates boot STALE code while gh pr merge
            # merges the real tip (the 2026-07-18 stale-worktree gate bug).
            # Best-effort: only resets when origin/<feature> resolves.
            from factory.chain.branch import feature_branch_name

            feat = db_story.github_branch or (
                feature_branch_name(db_story.github_issue_number, db_story.slug)
                if db_story.github_issue_number is not None
                else None
            )
            if feat:
                fetched = subprocess.run(
                    ["git", "fetch", "origin", feat],
                    cwd=str(wt), check=False, capture_output=True, timeout=60,
                )
                if fetched.returncode == 0:
                    subprocess.run(
                        ["git", "reset", "--hard", f"origin/{feat}"],
                        cwd=str(wt), check=False, capture_output=True, timeout=60,
                    )
            # Refresh the branch with the CURRENT base before gates run. A
            # PR that lingered through review cycles goes stale as siblings
            # merge; its smoke gate then boots an old backend against the
            # advanced shared-db schema and fails with false 500s (PR 226,
            # 2026-07-18: register 500 pre-merge, full green after merging
            # main). Conflict → abort and evaluate as-is (the conflicting-PR
            # path handles those). Best-effort throughout.
            base = cfg.default_branch or "main"
            subprocess.run(
                ["git", "fetch", "origin", base],
                cwd=str(wt), check=False, capture_output=True, timeout=60,
            )
            merged = subprocess.run(
                ["git", "merge", "--no-edit", f"origin/{base}"],
                cwd=str(wt), capture_output=True, timeout=120,
            )
            if merged.returncode != 0:
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=str(wt), check=False, capture_output=True, timeout=60,
                )
            return wt
        except Exception:
            return None

    if not fixtures and github_client is None:
        # No explicit fixtures and no GH client. Synthesize fixtures from
        # local StoryRecords that landed in a mergeable state — this is
        # the path the orchestrator's end-of-tick hook uses to surface
        # candidate merges from the chain itself.
        eng = _engine(db)
        with Session(eng) as session:
            mergeable_stories = session.exec(
                select(StoryRecord).where(
                    StoryRecord.app == app,
                    StoryRecord.state.in_(list(_MERGEABLE_STATES)),  # type: ignore[attr-defined]
                )
            ).all()
        for db_story in mergeable_stories:
            pr_no = db_story.github_pr_number
            if pr_no is None:
                # Docs chain may reach PR_OPEN in dry-run without a real
                # PR number; synthesize a placeholder so the worker still
                # records a decision row the operator can audit.
                pr_no = -(db_story.id or 0)
            # Point command gates (smoke-green boots the PR's OWN code) at
            # the story's chain worktree when it still exists. Without this,
            # repo_root=None made the required smoke gate unevaluable and
            # every synthesized PR sat unmergeable on 'missing smoke-green'
            # (observed 2026-07-18).
            fixtures.append(
                FixturePR(
                    pr_number=int(pr_no),
                    head_sha=f"local-{db_story.id}",
                    base_branch=cfg.default_branch or "main",
                    labels=[],
                    files_changed=[],
                    # Real CI conclusion via gh, never a hardcoded pass. Falls
                    # back to None (→ gate reads the recorded flag) for
                    # placeholder PR numbers or when no checks are configured.
                    ci_state=_query_ci_state(app_config=cfg, pr_number=int(pr_no)),
                    story=db_story,
                    repo_root=_story_worktree(root, app, db_story),
                )
            )
    if not fixtures and not dry_run and github_client is not None:  # pragma: no cover - real GH
        repo = github_client.get_repo(cfg.repo)
        for pr in repo.get_pulls(state="open"):
            # Resolve the StoryRecord for this PR, if any.
            eng = _engine(db)
            with Session(eng) as session:
                rows = session.exec(
                    select(StoryRecord).where(
                        StoryRecord.app == app, StoryRecord.github_pr_number == int(pr.number)
                    )
                ).all()
            story_row = rows[0] if rows else None
            fixtures.append(
                FixturePR(
                    pr_number=int(pr.number),
                    head_sha=str(pr.head.sha),
                    base_branch=str(pr.base.ref),
                    labels=[lbl.name for lbl in pr.labels],
                    files_changed=[f.filename for f in pr.get_files()],
                    ci_state=_query_ci_state(app_config=cfg, pr_number=int(pr.number)),
                    story=story_row,
                    repo_root=_story_worktree(root, app, story_row),
                )
            )

    actions: list[MergeAction] = []
    for f in fixtures:
        # CI-failure -> dev re-fix loop. Real CI reporting "failure" used to
        # just make the merge gate decline; now the failure is fed back to
        # dev BEFORE the normal gate/merge decision runs, so a story with a
        # broken PR gets a chance to actually converge instead of sitting in
        # PR_OPEN forever waiting for a human to notice. Guarded to real,
        # non-placeholder PRs in real-run only — synthesized-fixture/dry-run
        # tests (negative ``pr_number``, ``dry_run=True``) are unaffected.
        if (
            not dry_run
            and f.pr_number > 0
            and f.ci_state == "failure"
            and f.story is not None
            and f.story.state in _MERGEABLE_STATES
        ):
            redispatched = _handle_ci_failure(
                story=f.story,
                app_config=cfg,
                pr_number=f.pr_number,
                db=db,
                root=root,
            )
            if redispatched:
                action = MergeAction(
                    app=app,
                    pr_number=f.pr_number,
                    merged=False,
                    reason="real CI failed; story re-dispatched to dev for a fix",
                    gates_passed=[],
                    blocking_labels=[],
                )
                _record_merge_action(action, f.head_sha, db)
                actions.append(action)
                continue

        action = _evaluate_one_pr(
            app=app,
            fixture=f,
            app_config=cfg,
            dry_run=dry_run,
            github_client=github_client,
            merge_method=merge_method,
            wait_for_ci=wait_for_ci,
            delete_branch_after_merge=delete_branch_after_merge,
            software_factory_root=root,
            self_edit_gate=self_edit_gate,
            patch_provider=patch_provider,
            escalate=escalate,
            merge_fn=merge_fn,
            pr_merged_query=pr_merged_query,
        )
        _record_merge_action(action, f.head_sha, db)
        # A factory self-edit refused by the chain-side staging gate: the live
        # factory was never touched. Sink the story to a blocked/attention state
        # so the worker stops retrying it and an operator (or the FMS) picks it
        # up. The escalation was already emitted inside the gate.
        if action.staging_blocked and f.story is not None and f.story.state in _MERGEABLE_STATES:
            from factory.chain.state_machine import EVENT_PR_UNMERGEABLE, advance

            try:
                f.story.state = advance(f.story, EVENT_PR_UNMERGEABLE).value
                f.story.error = (
                    f"auto-merge refused a factory self-edit: PR #{action.pr_number} "
                    f"did not pass the chain-side staging gate "
                    f"({action.staging_status}). The live factory was NOT touched; "
                    f"escalated for human review."
                )
                eng = _engine(db)
                with Session(eng) as session:
                    session.add(f.story)
                    session.commit()
                    # Refresh while the session is open so the caller can still
                    # read ``f.story``'s attributes afterwards (commit() expires
                    # them → DetachedInstanceError once the session closes).
                    session.refresh(f.story)
            except Exception:  # noqa: BLE001 - state sink is best-effort
                pass
        # On a successful merge, enqueue a deploy candidate for the
        # post-merge deploy worker. The deploy itself runs in a separate
        # tick so the merge step stays focused.
        if action.merged:
            # Fire a context-refresh async — the BMAD context tree must
            # reflect the just-merged code, and we don't want the next
            # persona invocation reading stale ``context/project.md``.
            # The refresh runs in a background thread (or synchronously
            # in dry-run for deterministic tests) and is fully isolated
            # from the merge worker's return path.
            #
            # GATED OFF by default: the current refresh is a placeholder that
            # only appends a ``<!-- factory:context-refresh -->`` marker to
            # the same context/ files each merge, piling up as mutually
            # CONFLICTING orphan PRs with no merge path (they carry no
            # StoryRecord). Re-enable via AutoMergeConfig.context_refresh_enabled
            # once it's swapped for a real onboarder/tech_writer invocation.
            _refresh_on = False
            try:
                from factory.settings.loader import load_settings as _ls

                _refresh_on = _ls(root).auto_merge.context_refresh_enabled
            except Exception:
                _refresh_on = False
            if _refresh_on:
                try:
                    from factory.chain.context_refresh import schedule_post_merge_refresh

                    merged_scope = f.story.scope if f.story is not None else None
                    schedule_post_merge_refresh(
                        app=app,
                        merged_pr_number=action.pr_number,
                        merged_scope=merged_scope,
                        software_factory_root=root,
                        db_path=db,
                        sync=dry_run,
                        github_client=github_client,
                        # Dry-run uses synthetic fixtures with no real GH repo
                        # to push to; suppress PR open there so we exercise
                        # the worktree+commit path without network.
                        open_pr=not dry_run,
                    )
                except Exception:
                    # Never let a refresh failure poison the merge return.
                    # The refresh has its own event-log path; the merge worker
                    # cares about merging, not about context plumbing.
                    pass
            # Lazy import to avoid the auto-merge module importing the
            # deploy package at module load (the deploy package imports
            # back into the chain, which would risk a cycle).
            from factory.deploy.orchestrator import enqueue_deploy

            enqueue_deploy(
                app=app,
                sha=f.head_sha,
                merged_pr_number=action.pr_number,
                software_factory_root=root,
                db_path=db,
            )
            # Flip the story state to DEPLOY_PENDING so the orchestrator
            # tick picks up handle_deploy. Uses the state_machine's
            # advance() so an illegal transition surfaces loudly rather
            # than silently mis-updating the row.
            story = f.story
            if story is None:
                # Look up by PR number from the DB if the fixture didn't
                # carry one.
                eng = _engine(db)
                with Session(eng) as session:
                    rows = session.exec(
                        select(StoryRecord).where(
                            StoryRecord.app == app,
                            StoryRecord.github_pr_number == action.pr_number,
                        )
                    ).all()
                story = rows[0] if rows else None
            if story is not None and story.state in _MERGEABLE_STATES:
                from factory.chain.state_machine import EVENT_MERGED, advance

                try:
                    new_state = advance(story, EVENT_MERGED)
                    story.state = new_state.value
                    eng = _engine(db)
                    with Session(eng) as session:
                        session.add(story)
                        session.commit()
                        # Refresh while the session is still open so
                        # ``story``'s attributes stay readable afterwards
                        # (commit() expires them; without this, the
                        # sibling-cleanup read below raises
                        # DetachedInstanceError once the ``with`` block
                        # exits and the session closes).
                        session.refresh(story)
                except Exception:
                    # State-machine refusal is non-fatal here — the deploy
                    # queue entry still drives the work; the story will be
                    # reconciled by the orchestrator on a later tick.
                    pass
                else:
                    # Dual-draft cleanup: if ``story`` was one of two
                    # draft-alternative interpretations of the same
                    # direction, close the losing sibling's issue now that
                    # this one has won the merge (audit 2026-07-18, leak 4
                    # of 4 — the abandoned draft used to stay open
                    # forever). Best-effort/idempotent; never raises.
                    from factory.chain.dual_draft import close_abandoned_draft_sibling

                    close_abandoned_draft_sibling(
                        story, cfg, root, db, github_client, dry_run
                    )
        elif (
            not dry_run
            and not action.merged
            and action.reason.startswith("gh merge failed")
            and f.story is not None
            and f.story.state in _MERGEABLE_STATES
            and _pr_terminally_unmergeable(
                app_config=cfg, pr_number=action.pr_number, github_client=github_client
            )
        ):
            # The PR can never be merged AS-IS. Before sinking, make ONE safe
            # attempt to reconcile a merely-stale branch by merging the base in
            # (gh pr update-branch — no force-push). Many "un-mergeable" PRs are
            # only BEHIND a moved base, not truly conflicting; this recovers them
            # without human intervention. Gated to a single attempt per story via
            # the event log so a genuinely-conflicting PR can't loop forever.
            from factory.chain.event_log import log_story_event, read_story_events
            from factory.chain.state_machine import EVENT_PR_UNMERGEABLE, advance

            _already_tried = any(
                e.get("event") == "auto_merge_reconcile_attempt"
                for e in read_story_events(
                    f.story.id, software_factory_root=root, slug_hint=f.story.slug
                )
            )
            if not _already_tried and not dry_run and _attempt_pr_reconcile(
                app_config=cfg, pr_number=action.pr_number
            ):
                # Branch advanced — skip sinking; re-evaluate mergeability on the
                # next tick instead of blocking.
                try:
                    log_story_event(
                        f.story.id,
                        "auto_merge_reconcile_attempt",
                        {"pr_number": action.pr_number, "result": "branch_updated"},
                        software_factory_root=root,
                        slug_hint=f.story.slug,
                    )
                except Exception:  # noqa: BLE001
                    pass
            else:
                # Truly conflicting (or reconcile already tried). Sink so the
                # worker stops retrying and drive_chain can drain to DONE.
                try:
                    if not dry_run and not _already_tried:
                        log_story_event(
                            f.story.id,
                            "auto_merge_reconcile_attempt",
                            {"pr_number": action.pr_number, "result": "still_conflicting"},
                            software_factory_root=root,
                            slug_hint=f.story.slug,
                        )
                    f.story.state = advance(f.story, EVENT_PR_UNMERGEABLE).value
                    f.story.error = (
                        f"auto-merge gave up: PR #{action.pr_number} is terminally "
                        f"un-mergeable (closed/merged/conflicting) after a branch-"
                        f"update attempt. Needs regeneration or human resolution."
                    )
                    eng = _engine(db)
                    with Session(eng) as session:
                        session.add(f.story)
                        session.commit()
                except Exception:
                    pass
        # Emit auto_merge_attempt signal — best-effort, never raises.
        try:
            from factory.manager.signals import write_git_event as _wge_am

            _story_id_am: int | None = f.story.id if f.story is not None else None
            # An auto-merge that was ENABLED but is still awaiting required
            # checks is NOT an error — the merge is pending, not failed.
            # Reporting it as an error every tick would spam the L1 watcher
            # (concern-spam) for a healthy in-flight PR.
            _ok = action.merged or action.auto_merge_enabled
            _wge_am(
                kind="auto_merge_attempt",
                story_id=_story_id_am,
                pr_number=action.pr_number,
                result="ok" if _ok else "error",
                error=None if _ok else action.reason,
                software_factory_root=root,
            )
        except Exception:  # noqa: BLE001
            pass
        # Emit a dedicated pr_merge signal when the merge succeeded so L1
        # agents can distinguish "merge happened" from the broader
        # "auto_merge_attempt" record (which covers both attempts and
        # no-ops). commit_sha is not known post-squash-merge without a
        # GH API call; omit it here — the sha is derivable from git log
        # against the base branch if needed.
        if action.merged:
            try:
                from factory.manager.signals import write_git_event as _wge_pm

                _story_id_pm: int | None = f.story.id if f.story is not None else None
                _wge_pm(
                    kind="pr_merge",
                    story_id=_story_id_pm,
                    pr_number=action.pr_number,
                    result="ok",
                    software_factory_root=root,
                )
            except Exception:  # noqa: BLE001
                pass
        actions.append(action)
    return actions
