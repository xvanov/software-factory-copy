"""L2 of the factory self-improvement loop — turn improver proposals
into branches + PRs against the factory repo itself.

After ``run_factory_improver`` writes its JSON proposals, this module
classifies each proposal as ``safe`` / ``risky`` / ``invalid``,
applies the suggested patch on a fresh branch, runs the full factory
test suite, opens a PR, and auto-merges safe PRs via
``gh pr merge --squash --auto`` (GitHub's required-checks gate it).

Public entry points
===================

* ``classify_proposal`` — pure shape + safety check.
* ``apply_proposal`` — create branch, apply patch, run tests, commit,
  push. Returns ``ApplyResult``.
* ``open_pr_for_proposal`` — ``gh pr create`` with title/body/label.
* ``run_apply_pass`` — orchestrate the loop over a proposals JSON
  file. Returns ``ApplyPassSummary``.

Safety classifier — what counts as "safe"
-----------------------------------------

A proposal is ``safe`` only if ALL of:

* ``kind`` is ``prompt_edit`` or ``doc_update``.
* All paths the patch touches are under ``factory/personas/``, plus
  the top-level ``README.md`` / ``CLAUDE.md``.
* ``diff --stat`` ≤ 50 added, ≤ 30 deleted.
* No removed ``^#`` / ``^##`` heading lines.
* No newly added persona file (target must already exist).
* The full factory test suite passes locally after the patch.

Everything else → ``risky``. Proposals failing basic shape validation
(missing ``suggested_patch``, target file doesn't exist, patch
unparseable) → ``invalid`` and are dropped without a PR.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Classification = Literal["safe", "risky", "invalid"]
ApplyStatus = Literal[
    "applied",
    "queued_for_review",
    "abandoned",
    "invalid",
    "skipped_dry_run",
]

_SAFE_PATH_PREFIXES = ("factory/personas/",)
_SAFE_PATH_EXACT = ("README.md", "CLAUDE.md")
_SAFE_KINDS = ("prompt_edit", "doc_update")
_MAX_ADDED_LINES = 50
_MAX_DELETED_LINES = 30

SAFE_LABEL = "factory-self-improvement-safe"
REVIEW_LABEL = "factory-self-improvement-review"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    """Outcome of attempting to apply a single proposal."""

    proposal_index: int
    classification: Classification
    status: ApplyStatus
    branch: str | None = None
    pr_number: int | None = None
    tests_passed: bool | None = None
    error: str | None = None
    title: str | None = None
    label: str | None = None


@dataclass
class ApplyPassSummary:
    """Aggregated outcome of a full run_apply_pass over a proposals file."""

    applied: int = 0
    queued_for_review: int = 0
    abandoned: int = 0
    invalid: int = 0
    per_proposal: list[ApplyResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.applied + self.queued_for_review + self.abandoned + self.invalid


# ---------------------------------------------------------------------------
# Patch helpers
# ---------------------------------------------------------------------------


_DIFF_GIT_HEADER = re.compile(r"^diff --git ", re.MULTILINE)
_UNIFIED_HEADER = re.compile(r"^---\s+\S.*\n^\+\+\+\s+\S.*$", re.MULTILINE)


def _looks_like_unified_diff(patch: str) -> bool:
    """True iff ``patch`` looks like a unified-diff payload.

    We accept either a ``diff --git`` header (typical ``git diff``
    output) or a bare ``---``/``+++`` file-pair header (``diff -u``
    output). Free-text recipes won't match either.
    """
    if not patch or not patch.strip():
        return False
    if _DIFF_GIT_HEADER.search(patch):
        return True
    if _UNIFIED_HEADER.search(patch):
        return True
    return False


def _diff_target_paths(patch: str) -> list[str]:
    """Extract the list of file paths a unified diff touches.

    Strips the ``a/`` / ``b/`` prefixes ``git diff`` emits, and dedupes
    while preserving order so callers can present a stable list to the
    operator.
    """
    paths: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        p = raw
        if p.startswith("a/") or p.startswith("b/"):
            p = p[2:]
        if p and p != "/dev/null" and p not in seen:
            seen.add(p)
            paths.append(p)

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            # ``diff --git a/path/to/file b/path/to/file``
            # Extract BOTH the a/ (source) AND b/ (destination) sides. A pure
            # 100%-similarity rename carries NO ``+++`` hunk header, so the
            # destination path lives ONLY on this line — e.g. a rename INTO
            # factory/ (``diff --git a/apps/x.py b/factory/evil.py``) would
            # otherwise be seen only as ``apps/x.py`` and evade the self-edit /
            # forbidden-path detection that both the staging gate and the
            # forbidden guard rely on.
            parts = line.split()
            if len(parts) >= 4:
                _add(parts[2])  # a/ (source)
                _add(parts[3])  # b/ (destination — the rename/copy target)
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            _add(line[4:].strip())
    return paths


def _diff_line_counts(patch: str) -> tuple[int, int]:
    """Return ``(added, deleted)`` content-line counts for ``patch``.

    Hunk headers (``+++`` / ``---`` / ``@@``) are excluded; only
    in-hunk ``+`` / ``-`` lines count, matching ``git diff --stat``'s
    intent.
    """
    added = deleted = 0
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added, deleted


def _diff_removes_a_heading(patch: str) -> bool:
    """True iff any ``-`` line in the patch is a markdown ``#``/``##`` heading.

    Removing a section heading from a persona prompt is a load-bearing
    structural change — we never auto-merge it.
    """
    for line in patch.splitlines():
        if line.startswith("---"):
            continue
        if not line.startswith("-"):
            continue
        body = line[1:].lstrip()
        if body.startswith("#"):
            return True
    return False


def _diff_creates_new_file(patch: str) -> bool:
    """True iff the diff creates a new file (``--- /dev/null``)."""
    for line in patch.splitlines():
        if line.startswith("--- /dev/null"):
            return True
        if line.startswith("new file mode"):
            return True
    return False


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def _is_safe_path(path: str) -> bool:
    if path in _SAFE_PATH_EXACT:
        return True
    for prefix in _SAFE_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def classify_proposal(proposal: dict[str, Any], repo_path: Path) -> Classification:
    """Pure shape + safety check. No subprocess, no LLM, no I/O beyond
    a single ``Path.exists`` on the target.

    Returns:
      * ``"invalid"`` — proposal is missing required fields, target
        file doesn't exist, or the patch isn't a unified diff. Caller
        drops these silently.
      * ``"safe"`` — passes every safe-list check.
      * ``"risky"`` — well-formed but touches files we won't
        auto-merge.

    NB the *test-run* gate (the patch must keep the suite green) is
    enforced by ``apply_proposal``, not here — this function is pure.
    """
    if not isinstance(proposal, dict):
        return "invalid"
    kind = proposal.get("kind")
    target = proposal.get("target")
    patch = proposal.get("suggested_patch")
    if not isinstance(kind, str) or not isinstance(target, str):
        return "invalid"
    if not isinstance(patch, str) or not patch.strip():
        return "invalid"
    if not _looks_like_unified_diff(patch):
        return "invalid"

    paths = _diff_target_paths(patch)
    if not paths:
        return "invalid"
    # Target must exist when the diff isn't creating it from /dev/null.
    creates_new_file = _diff_creates_new_file(patch)
    if not creates_new_file:
        for p in paths:
            if not (repo_path / p).exists():
                return "invalid"

    if kind not in _SAFE_KINDS:
        return "risky"
    if creates_new_file:
        # Spec: "The patch does NOT add a NEW persona file (must edit
        # existing ones)." Any new file under a safe path is still
        # risky — operator should review whether the persona inventory
        # actually needs growing.
        return "risky"
    if not all(_is_safe_path(p) for p in paths):
        return "risky"
    added, deleted = _diff_line_counts(patch)
    if added > _MAX_ADDED_LINES or deleted > _MAX_DELETED_LINES:
        return "risky"
    if _diff_removes_a_heading(patch):
        return "risky"
    return "safe"


# ---------------------------------------------------------------------------
# Branch / patch / commit helpers
# ---------------------------------------------------------------------------


def _slugify(text: str, *, max_len: int = 40) -> str:
    """Lowercase, collapse non-alnum runs into ``-``, trim, clip."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return (s[:max_len] or "improvement").rstrip("-")


def branch_name_for(timestamp: int | str, rationale: str) -> str:
    """Compose ``factory-improver/<ts>-<short-slug>``.

    ``timestamp`` is whatever the caller threads through —
    ``run_apply_pass`` uses the proposals-file unix epoch. We don't
    care what the format is as long as it's filename-safe; the slug
    is derived from the rationale so multiple proposals from the
    same run get distinct branch names.
    """
    return f"factory-improver/{timestamp}-{_slugify(rationale)}"


def _run(
    args: list[str],
    *,
    cwd: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    timeout: int = 60,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess via the injected ``runner`` (default
    ``subprocess.run``). Centralised so tests can record every call."""
    proc = runner(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(args)} "
            f"rc={proc.returncode} stderr={(proc.stderr or '').strip()[:300]}"
        )
    return proc


def _current_branch(repo_path: Path, runner: Callable[..., Any]) -> str:
    proc = _run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path,
        runner=runner,
        timeout=15,
        check=True,
    )
    return (proc.stdout or "").strip()


def apply_proposal(
    proposal: dict[str, Any],
    repo_path: Path,
    *,
    proposal_index: int = 0,
    timestamp: int | str | None = None,
    classification: Classification | None = None,
    run_tests: bool = True,
    push: bool = True,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    test_command: list[str] | None = None,
) -> ApplyResult:
    """Apply ``proposal`` on a fresh branch off the current HEAD.

    Steps:
      1. Classify (unless caller pre-classified).
      2. Create ``factory-improver/<ts>-<slug>`` branch.
      3. ``git apply`` the suggested patch.
      4. Run the factory test suite (skippable for tests).
      5. ``git commit`` the change with a structured message.
      6. ``git push`` the branch (skippable).

    On any failure after the branch is created, we restore the
    starting branch and delete the half-built one so the worktree is
    left clean.
    """
    runner = runner or subprocess.run  # type: ignore[assignment]
    assert runner is not None  # for type-checkers
    classification = classification or classify_proposal(proposal, repo_path)
    if classification == "invalid":
        return ApplyResult(
            proposal_index=proposal_index,
            classification="invalid",
            status="invalid",
            error="failed_basic_validation",
        )

    rationale = str(proposal.get("rationale") or "improvement")
    kind = str(proposal.get("kind") or "improvement")
    ts = timestamp if timestamp is not None else "ts"
    branch = branch_name_for(ts, rationale)
    title = f"[factory-improver] {kind}: {_first_sentence(rationale)[:80]}"
    label = SAFE_LABEL if classification == "safe" else REVIEW_LABEL

    starting_branch: str | None = None
    try:
        starting_branch = _current_branch(repo_path, runner)
    except Exception as exc:  # noqa: BLE001
        return ApplyResult(
            proposal_index=proposal_index,
            classification=classification,
            status="abandoned",
            error=f"could_not_read_starting_branch: {exc!r}",
            title=title,
            label=label,
        )

    # Refuse to operate when tracked files have uncommitted edits —
    # ``git checkout -b`` would carry them into the new branch, and
    # our later ``git add <paths>`` could silently sweep them into
    # the commit. Untracked files (e.g. ``state/improvements/*.json``,
    # which is exactly where the proposals JSON we just wrote lives)
    # are fine because ``git add <paths>`` won't pick them up.
    diff_proc = _run(
        ["git", "diff", "--quiet", "HEAD", "--"],
        cwd=repo_path,
        runner=runner,
        timeout=15,
    )
    if diff_proc.returncode != 0:
        return ApplyResult(
            proposal_index=proposal_index,
            classification=classification,
            status="abandoned",
            error="dirty_working_tree",
            title=title,
            label=label,
        )

    def _cleanup() -> None:
        # Best-effort restore. We never raise from cleanup — the
        # caller already has an error to report.
        try:
            if starting_branch:
                _run(
                    ["git", "checkout", starting_branch],
                    cwd=repo_path,
                    runner=runner,
                    timeout=15,
                )
            _run(
                ["git", "branch", "-D", branch],
                cwd=repo_path,
                runner=runner,
                timeout=15,
            )
        except Exception:  # noqa: BLE001
            pass

    # 1. Create the branch.
    proc = _run(
        ["git", "checkout", "-b", branch],
        cwd=repo_path,
        runner=runner,
        timeout=15,
    )
    if proc.returncode != 0:
        return ApplyResult(
            proposal_index=proposal_index,
            classification=classification,
            status="abandoned",
            branch=branch,
            error=f"branch_create_failed: {(proc.stderr or '').strip()[:200]}",
            title=title,
            label=label,
        )

    # 2. Apply the patch.
    patch = str(proposal.get("suggested_patch") or "")
    patch_for_apply = patch if patch.endswith("\n") else patch + "\n"
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, encoding="utf-8"
    )
    try:
        tmp.write(patch_for_apply)
        tmp.flush()
        tmp.close()
        proc = _run(
            ["git", "apply", "--whitespace=nowarn", tmp.name],
            cwd=repo_path,
            runner=runner,
            timeout=30,
        )
    finally:
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except OSError:
            pass
    if proc.returncode != 0:
        _cleanup()
        return ApplyResult(
            proposal_index=proposal_index,
            classification=classification,
            status="abandoned",
            branch=branch,
            error=f"patch_apply_failed: {(proc.stderr or '').strip()[:300]}",
            title=title,
            label=label,
        )

    # 3. Run the factory test suite.
    tests_passed: bool | None = None
    if run_tests:
        cmd = test_command or ["uv", "run", "pytest", "-q", "--tb=no"]
        proc = _run(
            cmd,
            cwd=repo_path,
            runner=runner,
            timeout=600,
        )
        tests_passed = proc.returncode == 0
        if not tests_passed:
            _cleanup()
            return ApplyResult(
                proposal_index=proposal_index,
                classification=classification,
                status="abandoned",
                branch=branch,
                tests_passed=False,
                error="self_test_regression",
                title=title,
                label=label,
            )

    # 4. Commit.
    paths = _diff_target_paths(patch)
    if paths:
        proc = _run(
            ["git", "add", *paths],
            cwd=repo_path,
            runner=runner,
            timeout=15,
        )
    else:
        proc = _run(
            ["git", "add", "-u"],
            cwd=repo_path,
            runner=runner,
            timeout=15,
        )
    if proc.returncode != 0:
        _cleanup()
        return ApplyResult(
            proposal_index=proposal_index,
            classification=classification,
            status="abandoned",
            branch=branch,
            tests_passed=tests_passed,
            error=f"git_add_failed: {(proc.stderr or '').strip()[:200]}",
            title=title,
            label=label,
        )
    commit_msg = (
        f"auto: factory_improver applies {kind}\n\n"
        f"{rationale.strip()}\n\n"
        f"classification: {classification}\n"
        f"proposal_index: {proposal_index}\n"
    )
    proc = _run(
        ["git", "commit", "-m", commit_msg],
        cwd=repo_path,
        runner=runner,
        timeout=30,
    )
    if proc.returncode != 0:
        _cleanup()
        return ApplyResult(
            proposal_index=proposal_index,
            classification=classification,
            status="abandoned",
            branch=branch,
            tests_passed=tests_passed,
            error=f"git_commit_failed: {(proc.stderr or '').strip()[:200]}",
            title=title,
            label=label,
        )

    # 5. Push.
    if push:
        proc = _run(
            ["git", "push", "-u", "origin", branch],
            cwd=repo_path,
            runner=runner,
            timeout=120,
        )
        if proc.returncode != 0:
            # Don't clean up — the local commit is still useful for the
            # operator to inspect; just report the failure.
            return ApplyResult(
                proposal_index=proposal_index,
                classification=classification,
                status="abandoned",
                branch=branch,
                tests_passed=tests_passed,
                error=f"git_push_failed: {(proc.stderr or '').strip()[:200]}",
                title=title,
                label=label,
            )

    # Restore the starting branch so subsequent proposals branch off
    # the same base, not the just-applied branch.
    if starting_branch:
        _run(
            ["git", "checkout", starting_branch],
            cwd=repo_path,
            runner=runner,
            timeout=15,
        )

    status: ApplyStatus = "applied" if classification == "safe" else "queued_for_review"
    return ApplyResult(
        proposal_index=proposal_index,
        classification=classification,
        status=status,
        branch=branch,
        tests_passed=tests_passed,
        title=title,
        label=label,
    )


def _first_sentence(text: str) -> str:
    """First sentence of ``text`` (split on ``. ``), or whole text."""
    text = (text or "").strip().replace("\n", " ")
    if not text:
        return ""
    if "." in text:
        return text.split(".", 1)[0].strip()
    return text


# ---------------------------------------------------------------------------
# gh label + PR helpers
# ---------------------------------------------------------------------------


def ensure_labels_exist(
    repo: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Best-effort ``gh label create`` for the two improver labels.

    Idempotent: ``gh label create`` returns non-zero when the label
    already exists; we swallow that. Failures here never abort the
    apply pass.
    """
    runner = runner or subprocess.run  # type: ignore[assignment]
    assert runner is not None
    for name, color, desc in (
        (
            SAFE_LABEL,
            "0E8A16",
            "Auto-applied by factory_improver; squash-merged on green checks.",
        ),
        (
            REVIEW_LABEL,
            "FBCA04",
            "Risky factory_improver proposal — requires human review.",
        ),
    ):
        try:
            runner(
                [
                    "gh",
                    "label",
                    "create",
                    name,
                    "--repo",
                    repo,
                    "--color",
                    color,
                    "--description",
                    desc,
                    "--force",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except Exception:  # noqa: BLE001
            continue


def open_pr_for_proposal(
    proposal: dict[str, Any],
    apply_result: ApplyResult,
    repo: str,
    *,
    label: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    base: str = "main",
    auto_merge: bool | None = None,
) -> int | None:
    """Run ``gh pr create`` for the branch in ``apply_result``.

    Returns the PR number on success, or ``None`` on failure. The body
    embeds the rationale, the safety classification, and the
    suggested patch in a fenced code block so reviewers see what
    landed.

    ``auto_merge`` defaults to True when the classification is
    ``safe``. When True, we follow up with
    ``gh pr merge --squash --auto`` so the PR merges once required
    checks pass.
    """
    runner = runner or subprocess.run  # type: ignore[assignment]
    assert runner is not None
    if apply_result.branch is None:
        return None
    label = label or apply_result.label or REVIEW_LABEL
    if auto_merge is None:
        auto_merge = apply_result.classification == "safe"

    title = apply_result.title or f"[factory-improver] {proposal.get('kind', 'improvement')}"
    rationale = str(proposal.get("rationale") or "").strip()
    evidence = str(proposal.get("evidence") or "").strip()
    confidence = str(proposal.get("confidence") or "").strip()
    patch = str(proposal.get("suggested_patch") or "").strip()
    body_lines = [
        "Auto-generated by `factory_improver` (L2 apply pass).",
        "",
        f"- classification: **{apply_result.classification}**",
        f"- kind: `{proposal.get('kind', '?')}`",
        f"- target: `{proposal.get('target', '?')}`",
        f"- confidence: `{confidence or '?'}`",
        f"- evidence: `{evidence or '?'}`",
        f"- tests_passed: `{apply_result.tests_passed}`",
        "",
        "### Rationale",
        "",
        rationale or "_(none)_",
        "",
        "### Applied patch",
        "",
        "```diff",
        patch,
        "```",
    ]
    body = "\n".join(body_lines)

    proc = runner(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            base,
            "--head",
            apply_result.branch,
            "--title",
            title,
            "--body",
            body,
            "--label",
            label,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        return None
    m = re.search(r"/pull/(\d+)", proc.stdout or "")
    pr_number = int(m.group(1)) if m else None
    if pr_number is None:
        return None
    apply_result.pr_number = pr_number
    apply_result.label = label

    if auto_merge:
        runner(
            [
                "gh",
                "pr",
                "merge",
                str(pr_number),
                "--repo",
                repo,
                "--squash",
                "--auto",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    return pr_number


def close_pr_with_comment(
    pr_number: int,
    repo: str,
    *,
    comment: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Best-effort: post a comment then close the PR. Used when a
    safe-classified PR ends up failing tests post-apply."""
    runner = runner or subprocess.run  # type: ignore[assignment]
    assert runner is not None
    runner(
        ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body", comment],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    runner(
        ["gh", "pr", "close", str(pr_number), "--repo", repo],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _improver_self_edit_promotes(
    gate: Callable[..., Any],
    *,
    proposal: dict[str, Any],
    factory_root: Path,
    patch: str,
    proposal_index: int,
    log_event: Callable[[str, dict[str, Any]], None] | None,
) -> bool:
    """Validate a self-edit on a cloned factory before it may AUTO-MERGE.

    Wraps the improver's flat proposal dict into the shape
    ``staging.gate_self_edit`` expects (``proposal["proposal"]["suggested_patch"]``)
    and returns True ONLY when the clone ran healthy (``decision.promote``).

    Fail-safe: any exception (staging harness error) or a non-promote decision
    returns False, so the caller downgrades the PR to review-only (never
    auto-merges an unvalidated self-edit).
    """
    wrapped = {
        "proposal_id": f"improver-self-edit-{proposal_index}",
        "concern_title": str(
            proposal.get("rationale", "") or f"improver self-edit {proposal_index}"
        )[:80],
        "proposal": {"suggested_patch": patch},
    }
    proposal_path = f"improver:proposal-{proposal_index}"
    try:
        decision = gate(wrapped, proposal_path, root=factory_root)
    except Exception as exc:  # noqa: BLE001 - fail-safe: staging error → do not auto-merge
        if log_event:
            log_event(
                "factory_improver_staging_infra_failed",
                {"proposal_index": proposal_index, "error": repr(exc)[:200]},
            )
        return False
    promoted = bool(getattr(decision, "promote", False))
    if not promoted and log_event:
        log_event(
            "factory_improver_staging_blocked",
            {
                "proposal_index": proposal_index,
                "status": getattr(decision, "status", "staging_rejected"),
            },
        )
    return promoted


def run_apply_pass(
    proposals_json_path: Path,
    factory_root: Path,
    *,
    repo: str | None = None,
    run_tests: bool = True,
    push: bool = True,
    open_prs: bool = True,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    test_command: list[str] | None = None,
    log_event: Callable[[str, dict[str, Any]], None] | None = None,
    staging_gate: Callable[..., Any] | None = None,
) -> ApplyPassSummary:
    """Iterate over proposals in ``proposals_json_path``, classify +
    apply each, and return a summary.

    ``repo`` is the ``owner/name`` slug the PRs land in. When ``None``
    or ``open_prs=False``, the function still classifies and applies
    (useful for local dry-runs and tests).

    ``staging_gate`` is the self-edit staging validator (defaults to
    ``factory.manager.staging.gate_self_edit``). A ``"safe"`` proposal that
    edits the factory's OWN code (a self-edit, e.g. ``factory/personas/*.md``)
    is AUTO-MERGED (``gh pr merge --auto``) — so, exactly like the manager
    apply path, it must first be validated on a cloned factory. Without this,
    the L2 self-improver was a SECOND, ungated self-edit auto-merge surface. A
    non-self-edit safe proposal (``README.md`` / ``CLAUDE.md``) skips staging;
    a ``"risky"`` proposal was never auto-merged. Fail-safe: any non-promote
    outcome DOWNGRADES the PR to review-only (no ``--auto``).
    """
    summary = ApplyPassSummary()
    if not proposals_json_path.exists():
        return summary
    try:
        payload = json.loads(proposals_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return summary
    proposals = payload.get("improvements") or []
    if not isinstance(proposals, list):
        return summary

    # Derive a per-run timestamp slug from the proposals filename.
    ts_slug = proposals_json_path.stem.replace(":", "").replace("+", "_")

    if open_prs and repo:
        ensure_labels_exist(repo, runner=runner)

    for idx, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            summary.invalid += 1
            summary.per_proposal.append(
                ApplyResult(
                    proposal_index=idx,
                    classification="invalid",
                    status="invalid",
                    error="non_dict_proposal",
                )
            )
            continue

        classification = classify_proposal(proposal, factory_root)
        if classification == "invalid":
            summary.invalid += 1
            summary.per_proposal.append(
                ApplyResult(
                    proposal_index=idx,
                    classification="invalid",
                    status="invalid",
                    error="failed_basic_validation",
                )
            )
            if log_event:
                log_event(
                    "factory_improver_proposal_invalid",
                    {"proposal_index": idx, "target": proposal.get("target")},
                )
            continue

        result = apply_proposal(
            proposal,
            factory_root,
            proposal_index=idx,
            timestamp=ts_slug,
            classification=classification,
            run_tests=run_tests,
            push=push,
            runner=runner,
            test_command=test_command,
        )

        if result.status == "abandoned":
            summary.abandoned += 1
            summary.per_proposal.append(result)
            if log_event:
                log_event(
                    "factory_improver_self_test_failed"
                    if result.error == "self_test_regression"
                    else "factory_improver_apply_failed",
                    {
                        "proposal_index": idx,
                        "target": proposal.get("target"),
                        "branch": result.branch,
                        "error": result.error,
                    },
                )
            continue

        # Would this proposal auto-merge? Only "safe" ones do. A "safe" proposal
        # that is a SELF-EDIT (touches factory/**) must pass the staging gate on
        # a cloned factory before it may auto-merge — the same protection the
        # manager apply path has. This closes the second, previously-ungated
        # self-edit auto-merge surface. On any non-promote outcome we DOWNGRADE
        # to a review-only PR (auto_merge=False) so an unvalidated self-edit can
        # never auto-merge to the live factory. Non-self-edits (README/CLAUDE)
        # and risky proposals are unaffected.
        auto_merge = result.classification == "safe"
        staging_blocked = False
        if auto_merge:
            patch = proposal.get("suggested_patch", "")
            patch = patch if isinstance(patch, str) else ""
            paths = _diff_target_paths(patch)
            # Lazy import: staging imports _diff_target_paths from THIS module,
            # so a top-level import would be circular.
            from factory.manager.staging import gate_self_edit as _default_gate
            from factory.manager.staging import is_self_edit as _is_self_edit

            if _is_self_edit(paths):
                gate = staging_gate or _default_gate
                if not _improver_self_edit_promotes(
                    gate,
                    proposal=proposal,
                    factory_root=factory_root,
                    patch=patch,
                    proposal_index=idx,
                    log_event=log_event,
                ):
                    auto_merge = False
                    staging_blocked = True

        if open_prs and repo and result.branch:
            label = REVIEW_LABEL if staging_blocked else result.label
            pr = open_pr_for_proposal(
                proposal, result, repo, label=label, runner=runner, auto_merge=auto_merge
            )
            if pr is None:
                result.status = "abandoned"
                result.error = (result.error or "") + "; pr_create_failed"
                summary.abandoned += 1
                summary.per_proposal.append(result)
                continue

        # A staging-blocked self-edit is queued for human review, NOT applied —
        # even though it classified "safe" it was not (auto-)merged.
        if result.classification == "safe" and not staging_blocked:
            summary.applied += 1
        else:
            summary.queued_for_review += 1
        if staging_blocked:
            result.status = "queued_for_review"
        summary.per_proposal.append(result)

    return summary


def format_apply_pass_md(summary: ApplyPassSummary) -> str:
    """Markdown block summarising the apply pass for the pinned issue.

    Compact on purpose — the pinned issue is already long; we want
    counts + a quick per-proposal table the operator can scan.
    """
    lines = [
        "**L2 apply pass**",
        "",
        f"- applied (safe, auto-merge queued): **{summary.applied}**",
        f"- queued for review (risky PRs open): **{summary.queued_for_review}**",
        f"- abandoned (apply or self-test failed): **{summary.abandoned}**",
        f"- invalid (dropped, no PR): **{summary.invalid}**",
        "",
    ]
    if summary.per_proposal:
        lines.append("| # | classification | status | PR | branch |")
        lines.append("|---|---|---|---|---|")
        for r in summary.per_proposal:
            pr_cell = f"#{r.pr_number}" if r.pr_number else "—"
            branch_cell = f"`{r.branch}`" if r.branch else "—"
            lines.append(
                f"| {r.proposal_index} | {r.classification} | {r.status} | "
                f"{pr_cell} | {branch_cell} |"
            )
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "ApplyPassSummary",
    "ApplyResult",
    "REVIEW_LABEL",
    "SAFE_LABEL",
    "apply_proposal",
    "branch_name_for",
    "classify_proposal",
    "close_pr_with_comment",
    "ensure_labels_exist",
    "format_apply_pass_md",
    "open_pr_for_proposal",
    "run_apply_pass",
]
