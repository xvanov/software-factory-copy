"""Auto-merge worker — merges PRs that pass all 10 gates.

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
from factory.chain.gates.evaluator import ALL_GATE_LABELS, PRContext, evaluate_all_gates
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


def _evaluate_one_pr(
    *,
    app: str,
    fixture: FixturePR,
    app_config: AppConfig,
    dry_run: bool,
    github_client: Any,
) -> MergeAction:
    """Run all 10 gates against a fixture PR; return a MergeAction."""

    # Build the PRContext for the gate evaluator.
    pr_ctx = PRContext(
        pr_number=fixture.pr_number,
        head_sha=fixture.head_sha,
        base_branch=fixture.base_branch,
        files_changed=fixture.files_changed,
        labels=list(fixture.labels),
        ci_state=fixture.ci_state,
        repo_root=fixture.repo_root,
        story=fixture.story,
        dry_run=dry_run,
    )
    results = evaluate_all_gates(pr_ctx, app_config)
    gates_passed = [label for label, r in results.items() if r.passed]

    # Compute the labels the chain would have added on previous ticks. In
    # real-run we trust the actual PR labels; in dry-run we synthesize from
    # the gate results so a test fixture with all gates green is "all
    # labels present" without having to enumerate them in the fixture.
    if dry_run:
        present_labels = set(fixture.labels) | set(gates_passed)
    else:
        present_labels = set(fixture.labels)

    missing_labels = [label for label in ALL_GATE_LABELS if label not in present_labels]
    blocking_present = sorted(set(fixture.labels) & BLOCKING_LABELS)

    # Story state guard.
    story = fixture.story
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

    # All 10 gates passed + no blockers. Squash-merge.
    if not dry_run and github_client is not None:
        try:
            repo = github_client.get_repo(app_config.repo)
            pr = repo.get_pull(fixture.pr_number)
            pr.merge(merge_method="squash")
        except Exception as exc:  # pragma: no cover - real-run path
            return MergeAction(
                app=app,
                pr_number=fixture.pr_number,
                merged=False,
                reason=f"gh merge failed: {exc!r}",
                gates_passed=gates_passed,
                blocking_labels=blocking_present,
            )

    return MergeAction(
        app=app,
        pr_number=fixture.pr_number,
        merged=True,
        reason="all 10 gates passed; no blocking labels",
        gates_passed=gates_passed,
        blocking_labels=blocking_present,
    )


def auto_merge_tick(
    software_factory_root: Path,
    app: str,
    *,
    dry_run: bool = True,
    fixture_prs: list[FixturePR] | None = None,
    github_client: Any = None,
    db_path: Path | None = None,
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
                    repo_root=None,
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
        )
        _record_merge_action(action, f.head_sha, db)
        actions.append(action)
    return actions
