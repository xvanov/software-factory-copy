"""Rollback worker — watches main CI post-merge, opens revert PRs on red.

For each merge in ``merge_actions`` within the last ``window_minutes``:

  1. Check main branch CI status.
  2. If red, open a revert PR via the GH client.
  3. File a ``priority/p0`` regression issue capturing the failing test
     names + the merged PR link + the suspect commit.
  4. Flip the factory mode to ``fix-only`` so feature work pauses.
  5. Record the action in ``state/factory.db.rollback_actions``.

Dry-run: takes a ``fixture_ci_state_by_pr`` mapping so tests can drive
the worker without GH. Production calls ``github_client.get_repo(...)``
and reads the latest check_suite for ``app_config.default_branch``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Field, Session, SQLModel, create_engine, select

from factory.app_config import load_app_config
from factory.chain.auto_merge import MergeActionRecord
from factory.settings.modes import set_mode


class RollbackActionRecord(SQLModel, table=True):
    """One row per rollback decision."""

    __tablename__ = "rollback_actions"

    id: int | None = Field(default=None, primary_key=True)
    app: str = Field(index=True)
    merged_pr_number: int
    merged_head_sha: str
    action_type: str  # "revert" | "no_op"
    reason: str
    revert_pr_number: int | None = None
    regression_issue_number: int | None = None
    mode_after: str | None = None
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class RollbackAction:
    """Returned to the caller."""

    app: str
    pr_number: int
    head_sha: str
    action_type: str
    reason: str
    revert_pr_number: int | None = None
    regression_issue_number: int | None = None
    mode_after: str | None = None
    failing_tests: list[str] = field(default_factory=list)


def _engine(db_path: Path) -> Any:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(f"sqlite:///{db_path}", echo=False)
    SQLModel.metadata.create_all(eng)
    return eng


def _record_rollback(action: RollbackAction, db_path: Path) -> None:
    eng = _engine(db_path)
    rec = RollbackActionRecord(
        app=action.app,
        merged_pr_number=action.pr_number,
        merged_head_sha=action.head_sha,
        action_type=action.action_type,
        reason=action.reason,
        revert_pr_number=action.revert_pr_number,
        regression_issue_number=action.regression_issue_number,
        mode_after=action.mode_after,
    )
    with Session(eng) as session:
        session.add(rec)
        session.commit()


def _recent_merges(app: str, db_path: Path, window_minutes: int) -> list[MergeActionRecord]:
    """Read ``merge_actions`` for ``app`` within the last ``window_minutes``."""
    eng = _engine(db_path)
    cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)
    with Session(eng) as session:
        rows = session.exec(
            select(MergeActionRecord).where(
                MergeActionRecord.app == app,
                MergeActionRecord.merged == True,  # noqa: E712
            )
        ).all()
    out: list[MergeActionRecord] = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r.ts)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts >= cutoff:
            out.append(r)
    return out


def rollback_watch_tick(
    software_factory_root: Path,
    app: str,
    *,
    dry_run: bool = True,
    window_minutes: int = 15,
    fixture_ci_state_by_pr: dict[int, str] | None = None,
    fixture_failing_tests_by_pr: dict[int, list[str]] | None = None,
    github_client: Any = None,
    db_path: Path | None = None,
) -> list[RollbackAction]:
    """Single pass of the rollback watcher against ``app``.

    Returns one ``RollbackAction`` per recent merge. Tests pass
    ``fixture_ci_state_by_pr`` to drive the decision logic without GH.

    For each merge:
      * If main CI is green/pending → no_op.
      * If main CI is red → revert PR + p0 issue + mode flip.
    """
    root = Path(software_factory_root)
    db = db_path or (root / "state" / "factory.db")
    cfg = load_app_config(app, root)

    merges = _recent_merges(app, db, window_minutes)
    actions: list[RollbackAction] = []

    for m in merges:
        ci_state: str | None = None
        if fixture_ci_state_by_pr is not None:
            ci_state = fixture_ci_state_by_pr.get(m.pr_number)
        elif not dry_run and github_client is not None:  # pragma: no cover - real GH
            repo = github_client.get_repo(cfg.repo)
            commit = repo.get_commit(m.head_sha)
            ci_state = commit.get_combined_status().state

        if ci_state == "failure":
            failing_tests = (fixture_failing_tests_by_pr or {}).get(m.pr_number, [])
            revert_pr_number: int | None = None
            regression_issue_number: int | None = None
            if not dry_run and github_client is not None:  # pragma: no cover - real GH
                repo = github_client.get_repo(cfg.repo)
                # Open the revert PR. PyGithub doesn't expose ``gh pr revert``
                # directly; the closest is creating a branch + PR. The real
                # path uses the GH CLI binary because gh handles
                # cherry-pick-revert correctly.
                import subprocess

                subprocess.run(
                    ["gh", "pr", "revert", str(m.pr_number), "--repo", cfg.repo],
                    check=False,
                )
                # File the regression issue.
                issue = repo.create_issue(
                    title=f"[p0] Regression after merging PR #{m.pr_number}",
                    body=(
                        f"PR #{m.pr_number} (sha {m.head_sha}) merged and main CI went red.\n\n"
                        f"Failing tests:\n"
                        + "\n".join(f"- {t}" for t in failing_tests)
                        + "\n\nFactory mode auto-flipped to ``fix-only``."
                    ),
                    labels=["priority/p0", "regression"],
                )
                regression_issue_number = int(issue.number)
            else:
                # Dry-run: synthesize placeholder identifiers.
                revert_pr_number = 9000 + m.pr_number
                regression_issue_number = 8000 + m.pr_number

            mode_after = set_mode("fix-only", root, db_path=db)
            action = RollbackAction(
                app=app,
                pr_number=m.pr_number,
                head_sha=m.head_sha,
                action_type="revert",
                reason="main CI red after merge",
                revert_pr_number=revert_pr_number,
                regression_issue_number=regression_issue_number,
                mode_after=mode_after,
                failing_tests=failing_tests,
            )
        else:
            action = RollbackAction(
                app=app,
                pr_number=m.pr_number,
                head_sha=m.head_sha,
                action_type="no_op",
                reason=f"main CI ci_state={ci_state!r}",
            )
        _record_rollback(action, db)
        actions.append(action)

    return actions
