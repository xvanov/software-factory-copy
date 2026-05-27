"""factory.manager.apply — L4 Apply pipeline for manager proposals (Phase 6).

This module extends the existing ``factory_improver_apply`` pipeline to
consume ``state/manager_proposals/*.json`` produced by L3 (the Diagnostician).

Design note on the classifier
------------------------------
The classifier (``_classify_manager_proposal``) is the **only rule-based
component** of the FMS.  This is intentional and reflects a core design
principle:

  "LLMs are the basis. Heuristics are tools the LLMs call."

Detection, anomaly judgement, and proposal generation are all driven by
agents (L1/L2/L3).  The classifier exists here — as a deterministic,
LLM-free gate — because *applying patches to the live repo* requires hard
guarantees that the LLM layer cannot provide:

  * The LLM can be tricked, hallucinate paths, or propose patches that
    touch dangerous files.
  * The LLM's confidence score is not a safety guarantee.
  * We need a circuit-breaker that is auditable, testable, and that
    cannot be subverted by a crafted prompt.

Every other "judgment" in the system is LLM-driven.  This one is not,
and the code comments explain why.

Public entry points
====================

* ``_classify_manager_proposal`` — pure classification; no I/O except
  ``Path.exists`` on new-file paths.
* ``apply_manager_proposals`` — orchestrate the full L4 apply loop over
  ``state/manager_proposals/*.json``.  Returns a summary dict.

Classification rules (deterministic, no LLM)
---------------------------------------------

``"safe"``
  ``target_class ∈ {prompt_edit, persona_settings, detector_tool}``
  AND the patch passes class-specific validation:
  - prompt_edit: only factory/personas/*.md; no heading removal; ≤50+/≤30- lines
  - persona_settings: only routes.yaml or factory/personas/*.md; numeric clamp checks
  - detector_tool: only adds new factory/manager/detectors/*.py files or touches
    factory/manager/detectors/__init__.py; new files are valid Python

``"risky"``
  ``target_class == "dispatch_code"``  (always risky)
  OR any safe class whose patch fails class-specific validation

``"forbidden"``
  patch touches factory/manager/*.py (the manager editing itself)
  OR patch touches factory/chain/factory_improver_apply.py or this module
  OR escalate_to_human=true in the proposal

``"escalate_to_human"``
  proposal target_class == "escalate_to_human"
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory.chain.factory_improver_apply import (
    REVIEW_LABEL,
    SAFE_LABEL,
    ApplyResult,
    _diff_creates_new_file,
    _diff_line_counts,
    _diff_removes_a_heading,
    _diff_target_paths,
    _looks_like_unified_diff,
    _run,
    _slugify,
    open_pr_for_proposal,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MANAGER_APPLY_HISTORY = ".manager_apply_history.json"

# Safe class names whose patches we auto-classify as safe (subject to validation).
_SAFE_TARGET_CLASSES = {"prompt_edit", "persona_settings", "detector_tool"}

# Forbidden file patterns: any match → classified forbidden.
#
# Recursion safety (Phase 8):
# The first pattern was extended from ``^factory/manager/[^/]+\.py$`` (flat
# match only) to ``^factory/manager/.+\.py$`` (any depth).  This covers
# sub-directory files such as ``factory/manager/detectors/cost_spike.py``.
#
# CARVE-OUT for new detector files:
# The `_validate_detector_tool` validator allows the L3 Diagnostician to ADD
# new files under ``factory/manager/detectors/`` (the detector authorship
# loop).  The forbidden check below is applied BEFORE the class-specific
# validators, which would short-circuit new-detector proposals.  To preserve
# the carve-out, ``_any_path_is_forbidden`` explicitly skips the broad
# manager-subdir pattern for paths whose diffs are creating NEW files
# (--- /dev/null header).  This keeps the defence-in-depth against
# *modifying* existing sub-directory files while still allowing new detector
# additions.  See ``_path_is_forbidden_for_patch`` for the full logic.
_FORBIDDEN_PATH_PATTERNS = (
    re.compile(r"^factory/manager/.+\.py$"),           # manager/**/*.py (any depth)
    re.compile(r"^factory/chain/factory_improver_apply\.py$"),  # the old apply module
    re.compile(r"^factory/manager/apply\.py$"),        # this module itself (redundant with above, explicit)
)

# Sub-pattern that matches *only* manager sub-directory .py files (not the
# flat manager/*.py files which are ALWAYS forbidden regardless of new/modify).
# Used by the new-detector carve-out logic.
_MANAGER_SUBDIR_PATTERN = re.compile(r"^factory/manager/[^/]+/[^/]+\.py$")

# The flat manager/*.py pattern (always forbidden, no carve-out).
_MANAGER_FLAT_PATTERN = re.compile(r"^factory/manager/[^/]+\.py$")

# persona_settings: allowed numeric field names + their (min, max) clamps.
_PERSONA_NUMERIC_CLAMPS: dict[str, tuple[float, float]] = {
    "max_tokens": (4000, 65000),
    "temperature": (0.0, 1.5),
}

# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------


def _history_path(root: Path) -> Path:
    return root / "state" / _MANAGER_APPLY_HISTORY


def _load_history(root: Path) -> list[dict[str, Any]]:
    p = _history_path(root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _append_history(
    root: Path,
    entry: dict[str, Any],
) -> None:
    """Append one entry to the history file (create if absent)."""
    history = _load_history(root)
    history.append(entry)
    p = _history_path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(history, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        import sys
        print(f"[manager.apply] WARNING: failed to write history: {exc}", file=sys.stderr)


def _is_already_processed(root: Path, proposal_path: Path) -> bool:
    """Return True if this proposal_path already appears in the history."""
    history = _load_history(root)
    path_str = str(proposal_path)
    for entry in history:
        if entry.get("proposal_path") == path_str:
            return True
    return False


# ---------------------------------------------------------------------------
# Path-level forbidden check
# ---------------------------------------------------------------------------


def _path_is_forbidden(path: str) -> bool:
    """Return True if the given relative path matches any forbidden pattern.

    Use ``_path_is_forbidden_for_patch`` when you have a full patch available,
    because the new-detector carve-out requires inspecting the diff header.
    This simpler form is retained for backward compatibility; it treats all
    factory/manager/**/*.py as forbidden (no carve-out).
    """
    for pat in _FORBIDDEN_PATH_PATTERNS:
        if pat.match(path):
            return True
    return False


def _path_is_forbidden_in_patch(path: str, patch: str) -> bool:
    """Return True if *path* is forbidden given the context of *patch*.

    This implements the Phase 8 carve-out:
    - flat ``factory/manager/*.py`` → ALWAYS forbidden (no carve-out).
    - ``factory/manager/detectors/*.py`` (subdirectory) → forbidden UNLESS
      the diff for this path is creating a NEW file (--- /dev/null header).
      New detector files are handled by ``_validate_detector_tool``; modifying
      existing ones is forbidden as defence-in-depth.
    - Any other ``factory/manager/**/*.py`` sub-dir file → forbidden.
    - Everything else falls through to the standard forbidden-pattern check.
    """
    # Flat manager/*.py is always forbidden.
    if _MANAGER_FLAT_PATTERN.match(path):
        return True

    # Sub-directory manager file — apply carve-out for new detector files.
    if _MANAGER_SUBDIR_PATTERN.match(path):
        # Only carve out new files under factory/manager/detectors/.
        if re.match(r"^factory/manager/detectors/[^/]+\.py$", path):
            # If the patch CREATES this file (--- /dev/null), it goes through
            # _validate_detector_tool instead of being blocked here.
            if _file_is_created(patch, path):
                return False
        # All other subdirectory modifications → forbidden.
        return True

    # Check remaining forbidden patterns (e.g. factory_improver_apply.py).
    for pat in _FORBIDDEN_PATH_PATTERNS:
        if pat.match(path):
            return True
    return False


def _any_path_is_forbidden(paths: list[str]) -> bool:
    """Check forbidden without patch context (no carve-out)."""
    return any(_path_is_forbidden(p) for p in paths)


def _any_path_is_forbidden_in_patch(paths: list[str], patch: str) -> bool:
    """Check forbidden with patch context (supports new-detector carve-out)."""
    return any(_path_is_forbidden_in_patch(p, patch) for p in paths)


# ---------------------------------------------------------------------------
# Class-specific validators
# ---------------------------------------------------------------------------


def _validate_prompt_edit(patch: str, repo_root: Path) -> bool:  # noqa: ARG001
    """prompt_edit is safe only when the patch:
    - touches only factory/personas/*.md files
    - does not remove markdown headings
    - ≤50 added, ≤30 deleted lines
    - does not create new files (must edit existing ones)
    """
    paths = _diff_target_paths(patch)
    if not paths:
        return False
    for p in paths:
        if not re.match(r"^factory/personas/[^/]+\.md$", p):
            return False
    if _diff_creates_new_file(patch):
        return False
    added, deleted = _diff_line_counts(patch)
    if added > 50 or deleted > 30:
        return False
    if _diff_removes_a_heading(patch):
        return False
    return True


def _validate_persona_settings(patch: str, repo_root: Path) -> bool:  # noqa: ARG001
    """persona_settings is safe only when the patch:
    - touches only factory/routes.yaml or factory/personas/*.md
    - does not add/remove entire personas
    - numeric values in the diff are within allowed clamps
    - no novel numeric field names beyond the known schema
    """
    paths = _diff_target_paths(patch)
    if not paths:
        return False
    for p in paths:
        if p != "factory/routes.yaml" and not re.match(r"^factory/personas/[^/]+\.md$", p):
            return False

    # Check for out-of-range numeric values in added lines.
    for line in patch.splitlines():
        if not line.startswith("+"):
            continue
        if line.startswith("+++"):
            continue
        # Look for yaml key: value patterns on added lines.
        m = re.match(r"^\+\s*(\w+):\s+([0-9]+(?:\.[0-9]+)?)\s*$", line)
        if m:
            field_name = m.group(1)
            value = float(m.group(2))
            if field_name in _PERSONA_NUMERIC_CLAMPS:
                lo, hi = _PERSONA_NUMERIC_CLAMPS[field_name]
                if not (lo <= value <= hi):
                    return False
            else:
                # If it looks numeric but is an unknown field, it's novel → risky.
                # Only block if it's a top-level settings-like key.
                # We allow any non-numeric field freely; unknown numeric fields
                # are suspicious but we only block the known clamped ones.
                # Per spec: "Any other numeric persona setting: must be present in
                # the existing yaml schema (no novel fields)". We treat unknown
                # numeric fields as risky by failing validation.
                # Exception: common yaml like line numbers or ids are OK — we
                # only apply this check if the field name looks like a setting
                # (contains underscore or is all-lowercase alpha).
                if re.match(r"^[a-z_]+$", field_name) and len(field_name) > 2:
                    return False
    return True


def _validate_detector_tool(patch: str, repo_root: Path) -> bool:
    """detector_tool is safe only when the patch:
    - ONLY adds new files under factory/manager/detectors/*.py
      OR modifies factory/manager/detectors/__init__.py (the registry)
    - Does NOT modify existing detector files (other than __init__.py)
    - New files must be valid Python (checked via py_compile)
    - __init__.py: if modified, must only add import/registry lines
      (no arbitrary non-import code)
    """
    paths = _diff_target_paths(patch)
    if not paths:
        return False

    creates_new = _diff_creates_new_file(patch)
    adds_new_detector = False

    for p in paths:
        # Only allow additions under factory/manager/detectors/
        if re.match(r"^factory/manager/detectors/[^/]+\.py$", p):
            if p == "factory/manager/detectors/__init__.py":
                # Modifying __init__.py is OK only if we only add import/registry lines.
                if not _init_py_only_adds_imports(patch):
                    return False
            else:
                # Must be a NEW file; modifying an existing detector is risky.
                if creates_new:
                    adds_new_detector = True
                else:
                    # Check: is this path being created (--- /dev/null)?
                    if _file_is_created(patch, p):
                        adds_new_detector = True
                    else:
                        # Modifying existing detector → risky.
                        return False
        else:
            # Touching anything outside factory/manager/detectors/ → risky.
            return False

    if not adds_new_detector and not any(
        p == "factory/manager/detectors/__init__.py" for p in paths
    ):
        return False

    # For newly added files: check if they are valid Python.
    if adds_new_detector:
        if not _new_detector_files_are_valid_python(patch, repo_root):
            return False

    return True


def _file_is_created(patch: str, target_path: str) -> bool:
    """Check if a specific file path is being created (has --- /dev/null hunk)."""
    in_this_file = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            # Check if this diff block is about our target.
            if f"b/{target_path}" in line or f" {target_path} " in line:
                in_this_file = True
            else:
                in_this_file = False
        elif in_this_file and line.startswith("--- /dev/null"):
            return True
    return False


def _init_py_only_adds_imports(patch: str) -> bool:
    """Return True if every added (+) line in __init__.py changes is either
    blank, a comment, an import, a from-import, or a simple dict/list
    assignment (registry patterns). Disallow function/class definitions
    or complex control flow."""
    in_init = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            in_init = "__init__.py" in line
            continue
        if not in_init:
            continue
        if not line.startswith("+"):
            continue
        if line.startswith("+++"):
            continue
        content = line[1:].strip()
        if not content:
            continue
        if content.startswith("#"):
            continue
        if re.match(r"^(import |from )", content):
            continue
        # Allow: registry-style assignments like DETECTORS = {...}, "name": func
        if re.match(r"^[A-Z_]+ = ", content):
            continue
        if re.match(r'^["\'][a-z_]+["\']:\s+\w+', content):
            continue
        if content in ("{", "}", ")", "(", "[", "]", ","):
            continue
        if re.match(r"^\w+,?\s*$", content):
            continue
        # Anything else (def, class, if, for, etc.) is suspicious.
        if re.match(r"^(def |class |if |for |while |try:|except|with |raise )", content):
            return False
    return True


def _new_detector_files_are_valid_python(patch: str, repo_root: Path) -> bool:
    """Write each new detector file's content to a temp file and py_compile it.

    Returns True only if every new file compiles without error.
    """
    # Extract new file content from the patch.
    new_files: dict[str, list[str]] = {}  # path → lines
    current_file: str | None = None
    in_new_file = False

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current_file = None
            in_new_file = False
            # Extract path.
            parts = line.split()
            if len(parts) >= 4:
                p = parts[3]
                if p.startswith("b/"):
                    p = p[2:]
                if re.match(r"^factory/manager/detectors/[^/]+\.py$", p):
                    current_file = p
                    new_files[current_file] = []
        elif line.startswith("--- /dev/null") and current_file:
            in_new_file = True
        elif line.startswith("+++ ") and in_new_file:
            continue
        elif line.startswith("@@") and in_new_file:
            continue
        elif in_new_file and line.startswith("+") and not line.startswith("+++"):
            if current_file and current_file in new_files:
                new_files[current_file].append(line[1:])
        elif in_new_file and not line.startswith(("+", "-", " ", "\\")):
            # New diff block starting
            in_new_file = False

    if not new_files:
        return True  # No new files to check.

    for _path, content_lines in new_files.items():
        content = "\n".join(content_lines)
        try:
            tmp = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                delete=False,
                encoding="utf-8",
            )
            tmp.write(content)
            tmp.flush()
            tmp.close()
            result = subprocess.run(
                ["uv", "run", "python", "-m", "py_compile", tmp.name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            Path(tmp.name).unlink(missing_ok=True)
            if result.returncode != 0:
                return False
        except Exception:  # noqa: BLE001
            return False

    return True


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------


def _classify_manager_proposal(proposal: dict[str, Any], repo_root: Path) -> str:
    """Classify a manager proposal as 'safe', 'risky', 'forbidden', or
    'escalate_to_human'.

    This function is DETERMINISTIC and contains NO LLM calls.
    It is the only rule-based component of the FMS — apply-safety requires
    hard guarantees that the LLM layer upstream cannot provide.

    Returns one of:
      "safe"                — safe-class target + patch passes all validation
      "risky"               — well-formed but operator review required
      "forbidden"           — must never be applied automatically
      "escalate_to_human"   — explicit human-escalation request
    """
    if not isinstance(proposal, dict):
        return "forbidden"

    target_class = proposal.get("target_class", "")
    escalate = proposal.get("escalate_to_human", False)

    # Explicit escalation — check first before anything else.
    if target_class == "escalate_to_human" or escalate:
        return "escalate_to_human"

    # Extract the patch.
    inner = proposal.get("proposal", {})
    patch = inner.get("suggested_patch", "") if isinstance(inner, dict) else ""
    if not isinstance(patch, str):
        patch = ""

    # Validate patch format.
    if not patch.strip() or not _looks_like_unified_diff(patch):
        # No patch or non-diff → treat as forbidden (cannot apply safely).
        return "forbidden"

    paths = _diff_target_paths(patch)
    if not paths:
        return "forbidden"

    # Forbidden check: any path matching forbidden patterns → forbidden.
    # This is checked BEFORE target_class, so a "safe" target_class cannot
    # sneak in a patch that touches forbidden files.
    # Phase 8: use patch-context check so the new-detector carve-out applies.
    if _any_path_is_forbidden_in_patch(paths, patch):
        return "forbidden"

    # dispatch_code is always risky — operator must review any chain changes.
    if target_class == "dispatch_code":
        return "risky"

    # Phase 8: Manager persona file edits are risky (not safe), even when the
    # proposal claims a safe target_class.  The manager modifying its own persona
    # prompts is recursion bait — an operator should review these changes.
    # Pattern: factory/personas/manager_*.md
    if any(re.match(r"^factory/personas/manager_[^/]+\.md$", p) for p in paths):
        return "risky"

    # Safe-class validation.
    if target_class == "prompt_edit":
        if _validate_prompt_edit(patch, repo_root):
            return "safe"
        return "risky"

    if target_class == "persona_settings":
        if _validate_persona_settings(patch, repo_root):
            return "safe"
        return "risky"

    if target_class == "detector_tool":
        if _validate_detector_tool(patch, repo_root):
            return "safe"
        return "risky"

    # Unknown target_class → risky (not forbidden, since it passed the
    # forbidden-path check, but we won't auto-merge unknowns).
    return "risky"


# ---------------------------------------------------------------------------
# Branch naming for manager proposals
# ---------------------------------------------------------------------------


def _manager_branch_name(ts: str, title: str) -> str:
    return f"factory-manager/{ts}-{_slugify(title)}"


# ---------------------------------------------------------------------------
# Core apply logic for a single manager proposal
# ---------------------------------------------------------------------------


def _apply_one_manager_proposal(
    proposal: dict[str, Any],
    proposal_path: Path,
    root: Path,
    *,
    classification: str,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    test_command: list[str] | None = None,
    repo: str | None = None,
    open_prs: bool = True,
    push: bool = True,
) -> dict[str, Any]:
    """Apply a single classified manager proposal.

    Returns a result dict with keys:
      proposal_path, classification, status, branch, pr_url, pr_number, error
    """
    runner = runner or subprocess.run  # type: ignore[assignment]
    assert runner is not None

    ts_str = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    concern_title = proposal.get("concern_title", "unknown")
    branch = _manager_branch_name(ts_str, concern_title)

    result: dict[str, Any] = {
        "proposal_path": str(proposal_path),
        "ts": ts_str,
        "classification": classification,
        "status": "skipped_dry_run" if dry_run else "pending",
        "branch": branch,
        "pr_url": None,
        "pr_number": None,
        "error": None,
    }

    if dry_run:
        result["status"] = "skipped_dry_run"
        return result

    if classification in ("forbidden", "escalate_to_human"):
        # Record but do not apply.
        result["status"] = "forbidden" if classification == "forbidden" else "escalation_acknowledged"
        result["branch"] = None
        return result

    # Extract patch from inner proposal.
    inner = proposal.get("proposal", {})
    patch = inner.get("suggested_patch", "") if isinstance(inner, dict) else ""

    # Get repo root for git operations.
    from factory.chain.factory_improver_apply import (
        _current_branch,
        _diff_target_paths,
    )

    starting_branch: str | None = None
    try:
        starting_branch = _current_branch(root, runner)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "abandoned"
        result["error"] = f"could_not_read_starting_branch: {exc!r}"
        result["branch"] = None
        return result

    # Refuse dirty working tree (mirrors factory_improver_apply behaviour).
    diff_proc = _run(
        ["git", "diff", "--quiet", "HEAD", "--"],
        cwd=root,
        runner=runner,
        timeout=15,
    )
    if diff_proc.returncode != 0:
        result["status"] = "abandoned"
        result["error"] = "dirty_working_tree"
        result["branch"] = None
        return result

    def _cleanup() -> None:
        try:
            if starting_branch:
                _run(["git", "checkout", starting_branch], cwd=root, runner=runner, timeout=15)
            _run(["git", "branch", "-D", branch], cwd=root, runner=runner, timeout=15)
        except Exception:  # noqa: BLE001
            pass

    # 1. Create branch.
    proc = _run(["git", "checkout", "-b", branch], cwd=root, runner=runner, timeout=15)
    if proc.returncode != 0:
        result["status"] = "abandoned"
        result["error"] = f"branch_create_failed: {(proc.stderr or '').strip()[:200]}"
        return result

    # Wrap the rest of the apply steps in try/finally so that an unexpected
    # exception (e.g. subprocess.TimeoutExpired from _run) cannot leave the
    # working tree dirty or the branch dangling.
    _branch_created = True
    try:
        # 2. Apply patch.
        patch_for_apply = patch if patch.endswith("\n") else patch + "\n"
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False, encoding="utf-8")
        try:
            tmp.write(patch_for_apply)
            tmp.flush()
            tmp.close()
            proc = _run(
                ["git", "apply", "--whitespace=nowarn", tmp.name],
                cwd=root,
                runner=runner,
                timeout=30,
            )
        finally:
            Path(tmp.name).unlink(missing_ok=True)

        if proc.returncode != 0:
            _cleanup()
            _branch_created = False
            result["status"] = "abandoned"
            result["error"] = f"patch_apply_failed: {(proc.stderr or '').strip()[:300]}"
            return result

        # 3. Run test suite.
        cmd = test_command or ["uv", "run", "pytest", "-q", "--tb=no"]
        proc = _run(cmd, cwd=root, runner=runner, timeout=600)
        tests_passed = proc.returncode == 0
        result["tests_passed"] = tests_passed

        if not tests_passed:
            _cleanup()
            _branch_created = False
            result["status"] = "test_failed"
            result["error"] = "self_test_regression"
            return result

        # 4. Commit.
        kind = inner.get("kind", "improvement") if isinstance(inner, dict) else "improvement"
        rationale = inner.get("rationale", "") if isinstance(inner, dict) else ""
        commit_msg = (
            f"apply(fms): {kind} for {concern_title}\n\n"
            f"{rationale.strip()}\n\n"
            f"classification: {classification}\n"
            f"proposal_path: {proposal_path}\n\n"
            "Co-Authored-By: Factory Management System <noreply@factory>"
        )

        paths = _diff_target_paths(patch)
        if paths:
            proc = _run(["git", "add", *paths], cwd=root, runner=runner, timeout=15)
        else:
            proc = _run(["git", "add", "-u"], cwd=root, runner=runner, timeout=15)

        if proc.returncode != 0:
            _cleanup()
            _branch_created = False
            result["status"] = "abandoned"
            result["error"] = f"git_add_failed: {(proc.stderr or '').strip()[:200]}"
            return result

        proc = _run(["git", "commit", "-m", commit_msg], cwd=root, runner=runner, timeout=30)
        if proc.returncode != 0:
            _cleanup()
            _branch_created = False
            result["status"] = "abandoned"
            result["error"] = f"git_commit_failed: {(proc.stderr or '').strip()[:200]}"
            return result

        # Phase 8: record the manager-authored commit for circuit-breaker
        # tracking.  We use the branch HEAD SHA (the squash SHA isn't known
        # until after CI completes; branch HEAD is the canonical tracking key).
        try:
            sha_proc = _run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                runner=runner,
                timeout=10,
            )
            if sha_proc.returncode == 0:
                commit_sha = (sha_proc.stdout or "").strip()
                if commit_sha:
                    from factory.manager.circuit_breaker import record_manager_commit as _record_cb

                    _record_cb(root=root, sha=commit_sha, proposal_path=str(proposal_path))
        except Exception as _cb_record_exc:  # noqa: BLE001
            import sys
            print(
                f"[manager.apply] WARNING: failed to record manager commit for "
                f"circuit-breaker: {_cb_record_exc!r}",
                file=sys.stderr,
            )

        # 5. Push.
        if push:
            proc = _run(["git", "push", "-u", "origin", branch], cwd=root, runner=runner, timeout=120)
            if proc.returncode != 0:
                result["status"] = "abandoned"
                result["error"] = f"git_push_failed: {(proc.stderr or '').strip()[:200]}"
                return result

        # Restore starting branch.
        if starting_branch:
            _run(["git", "checkout", starting_branch], cwd=root, runner=runner, timeout=15)

        # 6. Open PR.
        if open_prs and repo and push:
            label = SAFE_LABEL if classification == "safe" else REVIEW_LABEL
            auto_merge = classification == "safe"

            # Build an apply_result-like object for open_pr_for_proposal.
            fake_result = ApplyResult(
                proposal_index=0,
                classification="safe" if classification == "safe" else "risky",  # type: ignore[arg-type]
                status="applied",
                branch=branch,
                tests_passed=tests_passed,
                title=f"[fms] {kind}: {concern_title[:60]}",
                label=label,
            )
            # Build a proposal-like dict for open_pr_for_proposal.
            proxy_proposal = {
                "kind": kind,
                "target": inner.get("target", "") if isinstance(inner, dict) else "",
                "rationale": rationale,
                "suggested_patch": patch,
                "confidence": inner.get("confidence", "") if isinstance(inner, dict) else "",
                "evidence": proposal.get("diagnosis", ""),
            }

            pr_number = open_pr_for_proposal(
                proxy_proposal,
                fake_result,
                repo,
                label=label,
                runner=runner,
                auto_merge=auto_merge,
            )

            if pr_number is None:
                result["status"] = "abandoned"
                result["error"] = "pr_create_failed"
                return result

            result["pr_number"] = pr_number
            result["status"] = "opened_pr"
        else:
            result["status"] = "applied" if classification == "safe" else "queued_for_review"

        return result

    except Exception:
        # Unexpected exception (e.g. subprocess.TimeoutExpired): clean up the
        # branch so the working tree is left in a known-good state, then re-raise.
        if _branch_created:
            _cleanup()
        raise


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def apply_manager_proposals(
    *,
    root: Path,
    dry_run: bool = False,
    proposal_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    test_command: list[str] | None = None,
    repo: str | None = None,
    open_prs: bool = True,
    push: bool = True,
) -> dict[str, Any]:
    """Apply manager proposals from ``state/manager_proposals/*.json``.

    Parameters
    ----------
    root:
        Factory root directory.
    dry_run:
        If True, classify proposals and log but do not apply any patches.
    proposal_path:
        If provided, apply only this specific proposal file.
    runner:
        Subprocess runner (injectable for tests).
    test_command:
        Override the test command.  Default: ``["uv", "run", "pytest", "-q", "--tb=no"]``.
    repo:
        GitHub ``owner/repo`` slug for PR creation.  When None or
        ``open_prs=False``, proposals are applied but no PRs are opened.
    open_prs:
        Whether to open GitHub PRs.  Default: True.
    push:
        Whether to ``git push`` branches.  Default: True.

    Returns
    -------
    dict
        Summary: ``{processed, safe_applied, risky_opened, forbidden,
        escalated_human, errors, results}``
    """
    root = Path(root)
    proposals_dir = root / "state" / "manager_proposals"

    # Phase 8: circuit-breaker guard — if the breaker is tripped, skip all
    # safe proposals until the operator resets it.  The halt_until window
    # gives the operator 24h to review and merge/discard the auto-revert PR.
    try:
        from factory.manager.circuit_breaker import get_state as _cb_get_state
        from factory.manager.circuit_breaker import is_tripped as _cb_is_tripped

        if _cb_is_tripped(root=root):
            cb_state = _cb_get_state(root=root) or {}
            return {
                "halted_by_circuit_breaker": True,
                "halt_until": cb_state.get("halt_until"),
                "regression_commit": cb_state.get("regression_commit"),
                "processed": 0,
                "safe_applied": 0,
                "risky_opened": 0,
                "forbidden": 0,
                "escalated_human": 0,
                "errors": [],
                "results": [],
            }
    except Exception as _cb_exc:  # noqa: BLE001
        import sys
        print(
            f"[manager.apply] WARNING: circuit-breaker check failed: {_cb_exc!r}; "
            "continuing with apply (fail-open).",
            file=sys.stderr,
        )

    summary: dict[str, Any] = {
        "processed": 0,
        "safe_applied": 0,
        "risky_opened": 0,
        "forbidden": 0,
        "escalated_human": 0,
        "errors": [],
        "results": [],
    }

    # Collect proposal files.
    if proposal_path is not None:
        paths = [Path(proposal_path)]
    else:
        if not proposals_dir.exists():
            return summary
        paths = sorted(proposals_dir.glob("*.json"))

    for p in paths:
        # Skip already-processed proposals.
        if _is_already_processed(root, p):
            continue

        try:
            proposal = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            summary["errors"].append(f"failed_to_load:{p.name}: {exc!r}")
            continue

        if not isinstance(proposal, dict):
            summary["errors"].append(f"non_dict_proposal:{p.name}")
            continue

        # Classify.
        classification = _classify_manager_proposal(proposal, root)

        # Apply.
        result = _apply_one_manager_proposal(
            proposal,
            p,
            root,
            classification=classification,
            dry_run=dry_run,
            runner=runner,
            test_command=test_command,
            repo=repo,
            open_prs=open_prs,
            push=push,
        )

        summary["processed"] += 1
        summary["results"].append(result)

        # Update counters.
        if classification == "safe" and result.get("status") in ("opened_pr", "applied"):
            summary["safe_applied"] += 1
        elif classification == "risky" and result.get("status") in ("opened_pr", "queued_for_review"):
            summary["risky_opened"] += 1
        elif classification == "forbidden":
            summary["forbidden"] += 1
        elif classification == "escalate_to_human":
            summary["escalated_human"] += 1

        if result.get("error"):
            summary["errors"].append(f"{p.name}: {result['error']}")

        # Record in history.
        history_entry = {
            "proposal_path": str(p),
            "ts": result.get("ts", datetime.now(UTC).isoformat()),
            "branch": result.get("branch"),
            "pr_url": result.get("pr_url"),
            "pr_number": result.get("pr_number"),
            "status": result.get("status", "unknown"),
            "classification": classification,
        }
        _append_history(root, history_entry)

    return summary


__all__ = [
    "_classify_manager_proposal",
    "apply_manager_proposals",
    "_load_history",
    "_is_already_processed",
]
