"""L2 apply pass — turns improver proposals into branches + PRs.

What we verify:
  * ``classify_proposal`` returns "safe" / "risky" / "invalid" under
    the exact conditions in the spec.
  * ``apply_proposal`` creates the branch, ``git apply``-s the patch,
    runs the test command, commits, and (when ``push=True``) pushes.
    On test failure it restores the starting branch and removes the
    half-built one.
  * ``run_apply_pass`` summary counts match the proposals fed in.
  * ``open_pr_for_proposal`` invokes ``gh pr create`` with the right
    title/body/label, and runs ``gh pr merge --squash --auto`` for
    safe proposals.
  * ``ensure_labels_exist`` is idempotent (we just don't crash when
    ``gh label create`` returns nonzero).
  * ``format_apply_pass_md`` renders a counts block plus a table.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from factory.chain.factory_improver_apply import (
    REVIEW_LABEL,
    SAFE_LABEL,
    ApplyPassSummary,
    ApplyResult,
    apply_proposal,
    branch_name_for,
    classify_proposal,
    ensure_labels_exist,
    format_apply_pass_md,
    open_pr_for_proposal,
    run_apply_pass,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


@dataclass
class _Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class _StagingStub:
    """Stand-in for ``staging.StagingDecision`` in staging-gate tests."""

    promote: bool
    status: str = "staging_validated"
    logs_tail: str = ""


def _promote_gate(proposal: Any, proposal_path: str, *, root: Any) -> _StagingStub:
    """Fake self-edit staging gate that always PROMOTES (clone ran healthy)."""
    return _StagingStub(promote=True)


def _reject_gate(proposal: Any, proposal_path: str, *, root: Any) -> _StagingStub:
    """Fake self-edit staging gate that REJECTS (a stage failed on the clone)."""
    return _StagingStub(promote=False, status="staging_rejected")


def _persona_diff_safe(rel_path: str = "factory/personas/dev.md") -> str:
    """Smallest realistic safe unified diff — adds one bullet line
    under an existing persona file."""
    return (
        f"diff --git a/{rel_path} b/{rel_path}\n"
        f"--- a/{rel_path}\n"
        f"+++ b/{rel_path}\n"
        "@@ -1,2 +1,3 @@\n"
        " # Persona\n"
        " body line\n"
        "+- new bullet added by improver\n"
    )


def _persona_diff_chain(rel_path: str = "factory/chain/handlers.py") -> str:
    return (
        f"diff --git a/{rel_path} b/{rel_path}\n"
        f"--- a/{rel_path}\n"
        f"+++ b/{rel_path}\n"
        "@@ -1,1 +1,2 @@\n"
        " body\n"
        "+# new line\n"
    )


def _make_repo_with_file(tmp_path: Path, rel_path: str, content: str) -> Path:
    """Create a real git repo containing one tracked file. Used for the
    integration-flavoured apply_proposal tests."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / rel_path).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel_path).write_text(content, encoding="utf-8")
    # Local-only git config — no global side effects.
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "commit.gpgsign", "false"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(args, cwd=str(repo), check=True, capture_output=True)
    return repo


# ---------------------------------------------------------------------------
# classify_proposal
# ---------------------------------------------------------------------------


def test_classify_safe_prompt_edit(tmp_path: Path) -> None:
    """A small prompt_edit unified diff under factory/personas/ is
    classified safe."""
    repo = _make_repo_with_file(tmp_path, "factory/personas/dev.md", "# Persona\nbody line\n")
    proposal = {
        "kind": "prompt_edit",
        "target": "factory/personas/dev.md",
        "rationale": "Add a clarifying bullet about forbidden paths.",
        "suggested_patch": _persona_diff_safe(),
    }
    assert classify_proposal(proposal, repo) == "safe"


def test_classify_risky_workflow_change_on_chain(tmp_path: Path) -> None:
    """A workflow_change touching factory/chain/* is risky even if
    small."""
    repo = _make_repo_with_file(tmp_path, "factory/chain/handlers.py", "body\n")
    proposal = {
        "kind": "workflow_change",
        "target": "factory/chain/handlers.py",
        "rationale": "Add a new gate.",
        "suggested_patch": _persona_diff_chain(),
    }
    assert classify_proposal(proposal, repo) == "risky"


def test_classify_risky_prompt_edit_but_chain_path(tmp_path: Path) -> None:
    """Kind says prompt_edit but the patch touches a forbidden path —
    risky, not safe."""
    repo = _make_repo_with_file(tmp_path, "factory/chain/handlers.py", "body\n")
    proposal = {
        "kind": "prompt_edit",
        "target": "factory/chain/handlers.py",
        "rationale": "Lying about kind.",
        "suggested_patch": _persona_diff_chain(),
    }
    assert classify_proposal(proposal, repo) == "risky"


def test_classify_invalid_missing_patch(tmp_path: Path) -> None:
    """A proposal with no ``suggested_patch`` is invalid."""
    proposal = {
        "kind": "prompt_edit",
        "target": "factory/personas/dev.md",
        "rationale": "missing patch",
    }
    assert classify_proposal(proposal, tmp_path) == "invalid"


def test_classify_invalid_missing_target(tmp_path: Path) -> None:
    """A proposal whose ``target`` field is absent is invalid."""
    proposal = {
        "kind": "prompt_edit",
        "rationale": "no target",
        "suggested_patch": _persona_diff_safe(),
    }
    assert classify_proposal(proposal, tmp_path) == "invalid"


def test_classify_invalid_target_file_missing(tmp_path: Path) -> None:
    """If the diff names a file that doesn't exist in the repo and the
    diff isn't creating it from /dev/null, the proposal is invalid."""
    repo = tmp_path / "repo"
    repo.mkdir()
    proposal = {
        "kind": "prompt_edit",
        "target": "factory/personas/dev.md",
        "rationale": "x",
        "suggested_patch": _persona_diff_safe(),
    }
    assert classify_proposal(proposal, repo) == "invalid"


def test_classify_invalid_non_diff_recipe(tmp_path: Path) -> None:
    """A free-text recipe (no diff headers) is invalid — can't be
    applied automatically."""
    proposal = {
        "kind": "prompt_edit",
        "target": "factory/personas/dev.md",
        "rationale": "x",
        "suggested_patch": "Append a paragraph about retry budget.",
    }
    assert classify_proposal(proposal, tmp_path) == "invalid"


def test_classify_risky_oversized_diff(tmp_path: Path) -> None:
    """A persona-touching diff with > 50 added lines is risky."""
    repo = _make_repo_with_file(
        tmp_path, "factory/personas/dev.md", "# Persona\nbody\n"
    )
    big_additions = "\n".join(f"+line {i}" for i in range(80))
    patch = (
        "diff --git a/factory/personas/dev.md b/factory/personas/dev.md\n"
        "--- a/factory/personas/dev.md\n"
        "+++ b/factory/personas/dev.md\n"
        "@@ -1,2 +1,82 @@\n"
        " # Persona\n"
        " body\n"
        f"{big_additions}\n"
    )
    proposal = {
        "kind": "prompt_edit",
        "target": "factory/personas/dev.md",
        "rationale": "oversized",
        "suggested_patch": patch,
    }
    assert classify_proposal(proposal, repo) == "risky"


def test_classify_risky_removes_heading(tmp_path: Path) -> None:
    """A diff that deletes a markdown heading is risky — load-bearing
    structural change."""
    repo = _make_repo_with_file(
        tmp_path,
        "factory/personas/dev.md",
        "# Persona\n## Section\nbody\n",
    )
    patch = (
        "diff --git a/factory/personas/dev.md b/factory/personas/dev.md\n"
        "--- a/factory/personas/dev.md\n"
        "+++ b/factory/personas/dev.md\n"
        "@@ -1,3 +1,2 @@\n"
        " # Persona\n"
        "-## Section\n"
        " body\n"
    )
    proposal = {
        "kind": "prompt_edit",
        "target": "factory/personas/dev.md",
        "rationale": "structural",
        "suggested_patch": patch,
    }
    assert classify_proposal(proposal, repo) == "risky"


def test_classify_risky_new_persona_file(tmp_path: Path) -> None:
    """A diff that creates a NEW persona file is risky — must edit
    existing ones."""
    repo = _make_repo_with_file(tmp_path, "factory/personas/dev.md", "x\n")
    patch = (
        "diff --git a/factory/personas/newbie.md b/factory/personas/newbie.md\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/factory/personas/newbie.md\n"
        "@@ -0,0 +1,2 @@\n"
        "+# Newbie\n"
        "+hello\n"
    )
    proposal = {
        "kind": "prompt_edit",
        "target": "factory/personas/newbie.md",
        "rationale": "new file",
        "suggested_patch": patch,
    }
    assert classify_proposal(proposal, repo) == "risky"


def test_classify_safe_readme(tmp_path: Path) -> None:
    """A doc_update touching README.md with a small diff is safe."""
    repo = _make_repo_with_file(tmp_path, "README.md", "# Readme\nbody\n")
    patch = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,2 +1,3 @@\n"
        " # Readme\n"
        " body\n"
        "+New line.\n"
    )
    proposal = {
        "kind": "doc_update",
        "target": "README.md",
        "rationale": "clarify usage",
        "suggested_patch": patch,
    }
    assert classify_proposal(proposal, repo) == "safe"


# ---------------------------------------------------------------------------
# apply_proposal — real git, mocked push + test
# ---------------------------------------------------------------------------


def _make_runner_with_capture(
    *,
    test_rc: int = 0,
    push_rc: int = 0,
) -> tuple[Callable[..., subprocess.CompletedProcess[Any]], list[list[str]]]:
    """Return a ``(runner, calls)`` pair. The runner intercepts
    ``pytest`` and ``git push`` calls (so we don't actually shell out
    to a real test suite or remote) but lets every other git command
    through to the real subprocess.run, so the working tree state is
    real."""
    calls: list[list[str]] = []

    def _runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        calls.append(list(args))
        # Intercept tests + push so we don't depend on the outside world.
        if args[:1] == ["uv"] and "pytest" in args:
            return _Completed(returncode=test_rc, stdout="captured")  # type: ignore[return-value]
        if args[:3] == ["git", "push", "-u"] or args[:2] == ["git", "push"]:
            return _Completed(returncode=push_rc, stdout="pushed")  # type: ignore[return-value]
        # Real git — strip ``check`` since we always pass ``check=False``.
        kwargs.pop("check", None)
        return subprocess.run(args, **kwargs)  # type: ignore[return-value]

    return _runner, calls


def test_apply_proposal_happy_path(tmp_path: Path) -> None:
    """Apply a safe prompt_edit on a real git repo:
    branch created, patch applied, tests run, commit made, push
    invoked. Working tree is left on the starting branch."""
    rel = "factory/personas/dev.md"
    repo = _make_repo_with_file(tmp_path, rel, "# Persona\nbody line\n")
    proposal = {
        "kind": "prompt_edit",
        "target": rel,
        "rationale": "Add a clarifying bullet about forbidden paths.",
        "suggested_patch": _persona_diff_safe(rel),
    }
    runner, calls = _make_runner_with_capture()

    result = apply_proposal(
        proposal,
        repo,
        proposal_index=0,
        timestamp="123",
        runner=runner,
    )

    assert isinstance(result, ApplyResult)
    assert result.classification == "safe"
    assert result.status == "applied"
    # Slug is clipped at 40 chars to keep branch names manageable.
    assert result.branch == "factory-improver/123-add-a-clarifying-bullet-about-forbidden"
    assert result.tests_passed is True
    assert result.error is None
    # The file was actually patched on the new branch's commit.
    log = subprocess.run(
        ["git", "log", "--oneline", "-1", result.branch],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "factory_improver applies prompt_edit" in log.stdout
    # Working tree restored to main.
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert head.stdout.strip() == "main"
    # Push was invoked.
    assert any(c[:2] == ["git", "push"] for c in calls)


def test_apply_proposal_aborts_when_tests_fail(tmp_path: Path) -> None:
    """When the test command returns nonzero after the patch, the
    branch is deleted and the worktree is back on main."""
    rel = "factory/personas/dev.md"
    repo = _make_repo_with_file(tmp_path, rel, "# Persona\nbody line\n")
    proposal = {
        "kind": "prompt_edit",
        "target": rel,
        "rationale": "Tweak that breaks tests",
        "suggested_patch": _persona_diff_safe(rel),
    }
    runner, calls = _make_runner_with_capture(test_rc=1)

    result = apply_proposal(
        proposal,
        repo,
        proposal_index=0,
        timestamp="123",
        runner=runner,
    )

    assert result.status == "abandoned"
    assert result.tests_passed is False
    assert result.error == "self_test_regression"
    assert result.branch is not None
    # Branch was deleted.
    branches = subprocess.run(
        ["git", "branch", "--list", result.branch],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert branches.stdout.strip() == "", f"branch should be gone, got: {branches.stdout!r}"
    # We did NOT push.
    assert not any(c[:2] == ["git", "push"] for c in calls)


def test_apply_proposal_aborts_on_patch_apply_failure(tmp_path: Path) -> None:
    """A diff whose context lines don't match the file is abandoned
    with ``patch_apply_failed`` and leaves the worktree on main."""
    rel = "factory/personas/dev.md"
    # File content does NOT match the patch's context lines.
    repo = _make_repo_with_file(tmp_path, rel, "totally different content\n")
    proposal = {
        "kind": "prompt_edit",
        "target": rel,
        "rationale": "bad patch context",
        "suggested_patch": _persona_diff_safe(rel),
    }
    runner, _calls = _make_runner_with_capture()

    result = apply_proposal(
        proposal,
        repo,
        proposal_index=0,
        timestamp="123",
        runner=runner,
        classification="safe",  # force-skip classifier so we actually try to apply
    )

    assert result.status == "abandoned"
    assert result.error is not None
    assert "patch_apply_failed" in result.error
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert head.stdout.strip() == "main"


def test_apply_proposal_refuses_dirty_tree(tmp_path: Path) -> None:
    """Uncommitted edits to *tracked* files → abandoned, no branch
    created. The operator's WIP must never get swept into an improver
    commit. (Untracked files are tolerated — that's where the
    proposals JSON lives in real runs.)"""
    rel = "factory/personas/dev.md"
    repo = _make_repo_with_file(tmp_path, rel, "# Persona\nbody line\n")
    # Modify a tracked file (the persona itself) so the working tree
    # diverges from HEAD.
    (repo / rel).write_text("# Persona\nbody line\nlocal edit\n", encoding="utf-8")
    proposal = {
        "kind": "prompt_edit",
        "target": rel,
        "rationale": "Should be refused.",
        "suggested_patch": _persona_diff_safe(rel),
    }
    runner, _calls = _make_runner_with_capture()

    result = apply_proposal(
        proposal,
        repo,
        proposal_index=0,
        timestamp="123",
        runner=runner,
    )
    assert result.status == "abandoned"
    assert result.error == "dirty_working_tree"
    # We did NOT create the branch.
    branches = subprocess.run(
        ["git", "branch", "--list"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "factory-improver/" not in branches.stdout


def test_apply_proposal_invalid_short_circuits(tmp_path: Path) -> None:
    """An invalid proposal returns immediately without touching git."""
    repo = _make_repo_with_file(tmp_path, "factory/personas/dev.md", "x\n")
    proposal = {"kind": "prompt_edit", "target": "factory/personas/dev.md"}  # no patch
    calls: list[list[str]] = []

    def _runner(args: list[str], **kwargs: Any) -> _Completed:
        calls.append(list(args))
        return _Completed(returncode=0)

    result = apply_proposal(proposal, repo, runner=_runner)
    assert result.classification == "invalid"
    assert result.status == "invalid"
    assert calls == []


# ---------------------------------------------------------------------------
# branch_name_for
# ---------------------------------------------------------------------------


def test_branch_name_for_slugifies_rationale() -> None:
    name = branch_name_for(123, "Tighten Dev's retry budget!!")
    assert name == "factory-improver/123-tighten-dev-s-retry-budget"


def test_branch_name_for_truncates_long_rationale() -> None:
    name = branch_name_for("ts", "x" * 200)
    assert len(name) <= len("factory-improver/ts-") + 40
    assert name.startswith("factory-improver/ts-")


# ---------------------------------------------------------------------------
# open_pr_for_proposal
# ---------------------------------------------------------------------------


def test_open_pr_for_safe_proposal_creates_pr_and_auto_merges() -> None:
    """A safe ApplyResult: ``gh pr create`` is called with the safe
    label, and ``gh pr merge --squash --auto`` is invoked."""
    calls: list[list[str]] = []

    def _runner(args: list[str], **kwargs: Any) -> _Completed:
        calls.append(list(args))
        if args[:3] == ["gh", "pr", "create"]:
            return _Completed(
                returncode=0,
                stdout="https://github.com/owner/repo/pull/55\n",
            )
        return _Completed(returncode=0)

    proposal = {
        "kind": "prompt_edit",
        "target": "factory/personas/dev.md",
        "rationale": "Tighten contract.",
        "suggested_patch": _persona_diff_safe(),
        "confidence": "high",
        "evidence": "log:foo",
    }
    apply_result = ApplyResult(
        proposal_index=0,
        classification="safe",
        status="applied",
        branch="factory-improver/123-tighten-contract",
        tests_passed=True,
        title="[factory-improver] prompt_edit: Tighten contract",
        label=SAFE_LABEL,
    )
    pr = open_pr_for_proposal(
        proposal, apply_result, "owner/repo", runner=_runner
    )
    assert pr == 55
    create_call = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert "--label" in create_call
    assert create_call[create_call.index("--label") + 1] == SAFE_LABEL
    assert "--head" in create_call
    assert create_call[create_call.index("--head") + 1] == apply_result.branch
    body_idx = create_call.index("--body")
    body = create_call[body_idx + 1]
    assert "classification: **safe**" in body
    assert "```diff" in body
    # Auto-merge follow-up.
    merge_call = next(c for c in calls if c[:3] == ["gh", "pr", "merge"])
    assert "--squash" in merge_call and "--auto" in merge_call
    assert "55" in merge_call


def test_open_pr_for_risky_proposal_no_auto_merge() -> None:
    """A risky proposal gets the review label and is NOT auto-merged."""
    calls: list[list[str]] = []

    def _runner(args: list[str], **kwargs: Any) -> _Completed:
        calls.append(list(args))
        if args[:3] == ["gh", "pr", "create"]:
            return _Completed(
                returncode=0,
                stdout="https://github.com/owner/repo/pull/77\n",
            )
        return _Completed(returncode=0)

    proposal = {
        "kind": "workflow_change",
        "target": "factory/chain/handlers.py",
        "rationale": "Risky thing.",
        "suggested_patch": _persona_diff_chain(),
    }
    apply_result = ApplyResult(
        proposal_index=1,
        classification="risky",
        status="queued_for_review",
        branch="factory-improver/123-risky-thing",
        tests_passed=True,
        title="[factory-improver] workflow_change: Risky thing",
        label=REVIEW_LABEL,
    )
    pr = open_pr_for_proposal(
        proposal, apply_result, "owner/repo", runner=_runner
    )
    assert pr == 77
    create_call = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert create_call[create_call.index("--label") + 1] == REVIEW_LABEL
    # No auto-merge call.
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_open_pr_returns_none_on_gh_failure() -> None:
    """When ``gh pr create`` fails, open_pr returns None."""

    def _runner(args: list[str], **kwargs: Any) -> _Completed:
        return _Completed(returncode=1, stderr="boom")

    apply_result = ApplyResult(
        proposal_index=0,
        classification="safe",
        status="applied",
        branch="factory-improver/x",
    )
    assert open_pr_for_proposal({}, apply_result, "owner/repo", runner=_runner) is None


# ---------------------------------------------------------------------------
# ensure_labels_exist
# ---------------------------------------------------------------------------


def test_ensure_labels_exist_invokes_gh_for_both() -> None:
    """``ensure_labels_exist`` calls ``gh label create`` twice — once
    per label — and tolerates nonzero returns."""
    calls: list[list[str]] = []

    def _runner(args: list[str], **kwargs: Any) -> _Completed:
        calls.append(list(args))
        # First call succeeds; second fails (already exists) — both fine.
        return _Completed(returncode=1 if "review" in args[3] else 0)

    ensure_labels_exist("owner/repo", runner=_runner)
    assert len(calls) == 2
    names = [c[3] for c in calls]
    assert SAFE_LABEL in names
    assert REVIEW_LABEL in names


# ---------------------------------------------------------------------------
# run_apply_pass
# ---------------------------------------------------------------------------


def test_run_apply_pass_counts_match(tmp_path: Path) -> None:
    """Three proposals (one safe, one risky, one invalid). The summary
    counts {applied: 1, queued_for_review: 1, invalid: 1, abandoned: 0}.
    Each non-invalid proposal goes through apply_proposal and PR
    creation, both mocked."""
    rel_persona = "factory/personas/dev.md"
    rel_chain = "factory/chain/handlers.py"
    # Build a repo with both files so the diffs apply.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "factory" / "personas").mkdir(parents=True)
    (repo / "factory" / "chain").mkdir(parents=True)
    (repo / rel_persona).write_text("# Persona\nbody line\n", encoding="utf-8")
    (repo / rel_chain).write_text("body\n", encoding="utf-8")
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "T"],
        ["git", "config", "commit.gpgsign", "false"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(args, cwd=str(repo), check=True, capture_output=True)

    proposals_path = repo / "state" / "improvements" / "42.json"
    proposals_path.parent.mkdir(parents=True)
    proposals_path.write_text(
        json.dumps(
            {
                "summary": "test",
                "events_processed": 0,
                "improvements": [
                    {
                        "kind": "prompt_edit",
                        "target": rel_persona,
                        "rationale": "Safe tweak.",
                        "suggested_patch": _persona_diff_safe(rel_persona),
                    },
                    {
                        "kind": "workflow_change",
                        "target": rel_chain,
                        "rationale": "Risky tweak.",
                        "suggested_patch": _persona_diff_chain(rel_chain),
                    },
                    {
                        "kind": "prompt_edit",
                        "target": rel_persona,
                        "rationale": "No patch — invalid",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    pr_counter = {"n": 100}

    def _runner(args: list[str], **kwargs: Any) -> Any:
        if args[:1] == ["uv"] and "pytest" in args:
            return _Completed(returncode=0)
        if args[:2] == ["git", "push"] or args[:3] == ["git", "push", "-u"]:
            return _Completed(returncode=0)
        if args[:3] == ["gh", "pr", "create"]:
            pr_counter["n"] += 1
            return _Completed(
                returncode=0,
                stdout=f"https://github.com/owner/repo/pull/{pr_counter['n']}\n",
            )
        if args[:3] == ["gh", "pr", "merge"]:
            return _Completed(returncode=0)
        if args[:3] == ["gh", "label", "create"]:
            return _Completed(returncode=0)
        kwargs.pop("check", None)
        return subprocess.run(args, **kwargs)

    summary = run_apply_pass(
        proposals_path,
        repo,
        repo="owner/repo",
        runner=_runner,
        # The safe proposal edits factory/personas/dev.md (a self-edit); inject
        # a promoting staging gate so it still auto-merges (applied) without
        # cloning a real factory.
        staging_gate=_promote_gate,
    )
    assert isinstance(summary, ApplyPassSummary)
    assert summary.applied == 1
    assert summary.queued_for_review == 1
    assert summary.abandoned == 0
    assert summary.invalid == 1
    assert summary.total == 3
    assert len(summary.per_proposal) == 3
    classifications = [r.classification for r in summary.per_proposal]
    assert classifications == ["safe", "risky", "invalid"]
    # PR numbers should have been assigned to the two non-invalid ones.
    assert summary.per_proposal[0].pr_number == 101
    assert summary.per_proposal[1].pr_number == 102
    assert summary.per_proposal[2].pr_number is None


def _readme_diff_safe(rel_path: str = "README.md") -> str:
    return (
        f"diff --git a/{rel_path} b/{rel_path}\n"
        f"--- a/{rel_path}\n"
        f"+++ b/{rel_path}\n"
        "@@ -1,2 +1,3 @@\n"
        " # Readme\n"
        " body\n"
        "+new line\n"
    )


def _init_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content, encoding="utf-8")
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "T"],
        ["git", "config", "commit.gpgsign", "false"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(args, cwd=str(repo), check=True, capture_output=True)
    return repo


def _recording_runner(merge_calls: list[list[str]]) -> Callable[..., Any]:
    """A runner that succeeds for the improver's shell-outs and records every
    ``gh pr merge`` invocation so tests can assert whether auto-merge fired."""

    def _runner(args: list[str], **kwargs: Any) -> Any:
        if args[:1] == ["uv"] and "pytest" in args:
            return _Completed(returncode=0)
        if args[:2] == ["git", "push"] or args[:3] == ["git", "push", "-u"]:
            return _Completed(returncode=0)
        if args[:3] == ["gh", "pr", "create"]:
            return _Completed(returncode=0, stdout="https://github.com/o/r/pull/7\n")
        if args[:3] == ["gh", "pr", "merge"]:
            merge_calls.append(args)
            return _Completed(returncode=0)
        if args[:3] == ["gh", "label", "create"]:
            return _Completed(returncode=0)
        kwargs.pop("check", None)
        return subprocess.run(args, **kwargs)

    return _runner


def _one_proposal(repo: Path, kind: str, target: str, patch: str) -> Path:
    proposals = repo / "state" / "improvements" / "9.json"
    proposals.parent.mkdir(parents=True, exist_ok=True)
    proposals.write_text(
        json.dumps(
            {"improvements": [{"kind": kind, "target": target, "rationale": "r", "suggested_patch": patch}]}
        ),
        encoding="utf-8",
    )
    return proposals


def test_run_apply_pass_self_edit_staging_promote_auto_merges(tmp_path: Path) -> None:
    """A safe self-edit (persona) whose staging clone runs healthy auto-merges."""
    rel = "factory/personas/dev.md"
    repo = _init_repo(tmp_path, {rel: "# Persona\nbody line\n"})
    proposals = _one_proposal(repo, "prompt_edit", rel, _persona_diff_safe(rel))
    merge_calls: list[list[str]] = []
    summary = run_apply_pass(
        proposals, repo, repo="o/r", runner=_recording_runner(merge_calls),
        staging_gate=_promote_gate,
    )
    assert summary.applied == 1
    assert summary.queued_for_review == 0
    # gh pr merge --auto DID fire (staging promoted the self-edit).
    assert any("--auto" in c for c in merge_calls)


def test_run_apply_pass_self_edit_staging_reject_blocks_auto_merge(tmp_path: Path) -> None:
    """A safe self-edit whose staging clone is unhealthy is DOWNGRADED to a
    review-only PR — never auto-merged to the live factory."""
    rel = "factory/personas/dev.md"
    repo = _init_repo(tmp_path, {rel: "# Persona\nbody line\n"})
    proposals = _one_proposal(repo, "prompt_edit", rel, _persona_diff_safe(rel))
    merge_calls: list[list[str]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    summary = run_apply_pass(
        proposals, repo, repo="o/r", runner=_recording_runner(merge_calls),
        staging_gate=_reject_gate,
        log_event=lambda k, p: events.append((k, p)),
    )
    # Not applied — queued for human review instead.
    assert summary.applied == 0
    assert summary.queued_for_review == 1
    # No auto-merge fired.
    assert not any("--auto" in c for c in merge_calls)
    # Staging block was surfaced as an event.
    assert any(k == "factory_improver_staging_blocked" for k, _ in events)


def test_run_apply_pass_self_edit_staging_infra_failure_blocks_auto_merge(tmp_path: Path) -> None:
    """A staging harness error is fail-safe: the self-edit is NOT auto-merged."""
    rel = "factory/personas/dev.md"
    repo = _init_repo(tmp_path, {rel: "# Persona\nbody line\n"})
    proposals = _one_proposal(repo, "prompt_edit", rel, _persona_diff_safe(rel))
    merge_calls: list[list[str]] = []
    events: list[tuple[str, dict[str, Any]]] = []

    def _boom_gate(proposal: Any, proposal_path: str, *, root: Any) -> Any:
        raise RuntimeError("copy repo unreachable")

    summary = run_apply_pass(
        proposals, repo, repo="o/r", runner=_recording_runner(merge_calls),
        staging_gate=_boom_gate,
        log_event=lambda k, p: events.append((k, p)),
    )
    assert summary.applied == 0
    assert summary.queued_for_review == 1
    assert not any("--auto" in c for c in merge_calls)
    assert any(k == "factory_improver_staging_infra_failed" for k, _ in events)


def test_run_apply_pass_non_self_edit_skips_staging(tmp_path: Path) -> None:
    """A safe NON-self-edit (README.md) auto-merges without consulting staging."""
    repo = _init_repo(tmp_path, {"README.md": "# Readme\nbody\n"})
    proposals = _one_proposal(repo, "doc_update", "README.md", _readme_diff_safe())
    merge_calls: list[list[str]] = []
    gate_calls = {"n": 0}

    def _counting_gate(proposal: Any, proposal_path: str, *, root: Any) -> _StagingStub:
        gate_calls["n"] += 1
        return _StagingStub(promote=True)

    summary = run_apply_pass(
        proposals, repo, repo="o/r", runner=_recording_runner(merge_calls),
        staging_gate=_counting_gate,
    )
    assert summary.applied == 1
    # README is not under factory/ → not a self-edit → staging never consulted.
    assert gate_calls["n"] == 0
    assert any("--auto" in c for c in merge_calls)


def test_run_apply_pass_missing_file_returns_empty(tmp_path: Path) -> None:
    """A missing proposals JSON yields an empty summary, no crash."""
    summary = run_apply_pass(
        tmp_path / "does-not-exist.json", tmp_path, repo=None
    )
    assert summary.total == 0
    assert summary.per_proposal == []


def test_run_apply_pass_logs_self_test_failure(tmp_path: Path) -> None:
    """When apply_proposal abandons due to test regression,
    run_apply_pass invokes ``log_event`` with
    ``factory_improver_self_test_failed``."""
    rel = "factory/personas/dev.md"
    repo = _make_repo_with_file(tmp_path, rel, "# Persona\nbody line\n")
    proposals = repo / "state" / "improvements" / "1.json"
    proposals.parent.mkdir(parents=True)
    proposals.write_text(
        json.dumps(
            {
                "improvements": [
                    {
                        "kind": "prompt_edit",
                        "target": rel,
                        "rationale": "Breaks tests.",
                        "suggested_patch": _persona_diff_safe(rel),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def _runner(args: list[str], **kwargs: Any) -> Any:
        if args[:1] == ["uv"] and "pytest" in args:
            return _Completed(returncode=1, stdout="failures")
        if args[:2] == ["git", "push"]:
            return _Completed(returncode=0)
        kwargs.pop("check", None)
        return subprocess.run(args, **kwargs)

    events: list[tuple[str, dict[str, Any]]] = []

    summary = run_apply_pass(
        proposals,
        repo,
        repo=None,  # no PR creation needed
        runner=_runner,
        log_event=lambda kind, payload: events.append((kind, payload)),
    )
    assert summary.abandoned == 1
    assert events and events[0][0] == "factory_improver_self_test_failed"


# ---------------------------------------------------------------------------
# format_apply_pass_md
# ---------------------------------------------------------------------------


def test_format_apply_pass_md_contains_counts_and_table() -> None:
    summary = ApplyPassSummary(
        applied=1,
        queued_for_review=2,
        abandoned=0,
        invalid=1,
        per_proposal=[
            ApplyResult(
                proposal_index=0,
                classification="safe",
                status="applied",
                branch="factory-improver/x",
                pr_number=10,
            ),
            ApplyResult(
                proposal_index=1,
                classification="risky",
                status="queued_for_review",
                branch="factory-improver/y",
                pr_number=11,
            ),
        ],
    )
    md = format_apply_pass_md(summary)
    assert "applied (safe, auto-merge queued): **1**" in md
    assert "queued for review (risky PRs open): **2**" in md
    assert "invalid (dropped, no PR): **1**" in md
    assert "| 0 | safe | applied | #10 |" in md
    assert "factory-improver/y" in md


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
