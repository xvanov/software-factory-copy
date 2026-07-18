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

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Field, Session, SQLModel, create_engine, select

from factory.app_config import AppConfig, load_app_config
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

# Gate labels the docs chain produces. The docs chain skips the TDD
# red→green loop, so the 10 TDD gate labels don't apply. The single
# label the docs chain DOES enforce is ``canonical-paths-only`` — when
# ``handle_docs_enforcer`` reaches PR_OPEN with no violations, the
# canonical-paths gate has effectively passed for the PR.
#
# Kept as a frozenset so the worker can swap the gate set on
# ``chain_kind`` without re-checking the TDD list.
_DOCS_CHAIN_GATE_LABELS: frozenset[str] = frozenset({"canonical-paths-only"})


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
) -> MergeAction:
    """Decide if a PR should be merged; merge it in real-run.

    Branches on ``story.chain_kind``:

    * ``tdd`` (or unknown): the historical 10-gate check.
    * ``docs``: the docs chain skips the TDD red→green loop and
      doesn't apply the 10 TDD labels; the canonical-paths enforcer
      runs before reaching PR_OPEN, so we only check
      mergeable-state + blocking-labels.
    """
    story = fixture.story
    docs_chain = _is_docs_chain(story)

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
            for label in required_gate_labels(app_config)
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

    # Gates passed + no blockers. Merge.
    if not dry_run:
        merge_err = _gh_pr_merge(
            app_config=app_config,
            pr_number=fixture.pr_number,
            merge_method=merge_method,
            wait_for_ci=wait_for_ci,
            delete_branch=delete_branch_after_merge,
            github_client=github_client,
        )
        if merge_err is not None:  # pragma: no cover - real-run path
            return MergeAction(
                app=app,
                pr_number=fixture.pr_number,
                merged=False,
                reason=f"gh merge failed: {merge_err}",
                gates_passed=gates_passed,
                blocking_labels=blocking_present,
            )

    reason = (
        "docs chain enforcer passed; no blocking labels"
        if docs_chain
        else "all required gates passed; no blocking labels"
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
) -> list[MergeAction]:
    """Single pass of the auto-merge worker against ``app``.

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
        """The story's chain worktree, if it still exists — command gates
        (smoke-green boots the PR's OWN code) need a local tree to run in.
        Worktrees are keyed by GITHUB ISSUE NUMBER, not the db row id (see
        handlers._writing_worktree). Without this, repo_root=None made the
        required smoke gate unevaluable and every PR sat unmergeable on
        'missing smoke-green' (observed 2026-07-18)."""
        if db_story is None:
            return None
        try:
            from factory.chain.worktree import worktree_path

            cand = worktree_path(root, app, db_story.github_issue_number, db_story.slug)
            return cand if cand.exists() else None
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
                    ci_state="success",
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
                    ci_state=None,
                    story=story_row,
                    repo_root=_story_worktree(root, app, story_row),
                )
            )

    actions: list[MergeAction] = []
    for f in fixtures:
        action = _evaluate_one_pr(
            app=app,
            fixture=f,
            app_config=cfg,
            dry_run=dry_run,
            github_client=github_client,
            merge_method=merge_method,
            wait_for_ci=wait_for_ci,
            delete_branch_after_merge=delete_branch_after_merge,
        )
        _record_merge_action(action, f.head_sha, db)
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
                except Exception:
                    # State-machine refusal is non-fatal here — the deploy
                    # queue entry still drives the work; the story will be
                    # reconciled by the orchestrator on a later tick.
                    pass
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
            _wge_am(
                kind="auto_merge_attempt",
                story_id=_story_id_am,
                pr_number=action.pr_number,
                result="ok" if action.merged else "error",
                error=None if action.merged else action.reason,
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
