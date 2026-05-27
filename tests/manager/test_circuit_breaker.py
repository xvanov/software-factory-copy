"""Tests for factory.manager.circuit_breaker (Phase 8).

Coverage:
  - record_manager_commit appends to ndjson.
  - check_and_trip: no failure → no action.
  - check_and_trip: failure but no manager commits → None.
  - check_and_trip: failure + manager commit at HEAD → trips.
  - is_tripped: within halt window → True.
  - is_tripped: after halt window → False.
  - apply refuses when tripped.
  - reset archives to history.
  - classify: manager persona edit → risky.
  - classify: existing manager subdir file → forbidden.
  - classify: new detector file (carve-out) → safe.
  - classify: factory/manager/halt.py → forbidden.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch as _patch

import pytest

from factory.manager.apply import _classify_manager_proposal, apply_manager_proposals
from factory.manager.circuit_breaker import (
    _load_manager_commits,
    check_and_trip,
    get_state,
    is_tripped,
    record_manager_commit,
    reset,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
# FUTURE_HALT is far in the future so real-wall-clock tests don't expire
FUTURE_HALT = "2099-12-31T23:59:59+00:00"
PAST_HALT = (NOW - timedelta(hours=1)).isoformat()


def _make_repo(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """Create a minimal git repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    if files:
        for rel, content in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "commit.gpgsign", "false"],
        ["git", "add", "."] if files else None,
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        if args is None:
            continue
        subprocess.run(args, cwd=str(repo), check=True, capture_output=True)
    return repo


def _write_tripped_state(root: Path, halt_until: str, sha: str = "abc123") -> None:
    """Write a circuit-breaker state file directly (for is_tripped tests)."""
    state = {
        "schema_version": 1,
        "tripped_at": NOW.isoformat(),
        "regression_commit": sha,
        "regression_commit_message": "test commit",
        "revert_branch": "factory-manager-revert/test",
        "revert_pr_number": None,
        "test_output_excerpt": "test failed",
        "halt_until": halt_until,
    }
    cb_file = root / "state" / "circuit_breaker.json"
    cb_file.parent.mkdir(parents=True, exist_ok=True)
    cb_file.write_text(json.dumps(state), encoding="utf-8")


# ---------------------------------------------------------------------------
# record_manager_commit
# ---------------------------------------------------------------------------


def test_record_manager_commit_appends_to_ndjson(tmp_path: Path) -> None:
    """record_manager_commit twice → ndjson has 2 lines."""
    root = tmp_path / "root"
    root.mkdir()

    record_manager_commit(root=root, sha="sha1", proposal_path="/some/path.json")
    record_manager_commit(root=root, sha="sha2", proposal_path="/another/path.json")

    path = root / "state" / ".manager_commits.ndjson"
    assert path.exists()
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2

    rec1 = json.loads(lines[0])
    rec2 = json.loads(lines[1])
    assert rec1["sha"] == "sha1"
    assert rec2["sha"] == "sha2"
    assert rec1["proposal_path"] == "/some/path.json"

    # _load_manager_commits returns both.
    commits = _load_manager_commits(root)
    assert len(commits) == 2
    assert {c["sha"] for c in commits} == {"sha1", "sha2"}


# ---------------------------------------------------------------------------
# check_and_trip — no failure
# ---------------------------------------------------------------------------


def test_check_and_trip_no_failure_no_action(tmp_path: Path) -> None:
    """If test_command returns 0, check_and_trip returns None."""
    root = tmp_path / "root"
    root.mkdir()
    # Plant a tracked commit.
    record_manager_commit(root=root, sha="sha_passing", proposal_path="/p.json")

    # Patch subprocess.run to return success.
    with _patch("factory.manager.circuit_breaker.subprocess.run") as mock_run:
        mock_run.return_value = type("CP", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
        result = check_and_trip(root=root, now=NOW)

    assert result is None
    # No circuit_breaker.json should exist.
    assert not (root / "state" / "circuit_breaker.json").exists()


# ---------------------------------------------------------------------------
# check_and_trip — failure but no manager commits
# ---------------------------------------------------------------------------


def test_check_and_trip_failure_no_manager_commits_returns_none(tmp_path: Path) -> None:
    """Tests fail but no tracked manager commits → breaker not tripped."""
    root = tmp_path / "root"
    root.mkdir()
    # No commits registered.

    def _mock_run(cmd, **kwargs):
        @dataclass
        class _CP:
            returncode: int
            stdout: str = ""
            stderr: str = ""

        # Tests fail.
        if kwargs.get("shell"):
            return _CP(returncode=1, stdout="", stderr="FAILED 3 failed")
        # git rev-parse HEAD
        return _CP(returncode=0, stdout="deadbeef1234")

    with _patch("factory.manager.circuit_breaker.subprocess.run", side_effect=_mock_run):
        result = check_and_trip(root=root, now=NOW)

    assert result is None
    assert not (root / "state" / "circuit_breaker.json").exists()


# ---------------------------------------------------------------------------
# check_and_trip — failure with manager commit → trips
# ---------------------------------------------------------------------------


def test_check_and_trip_failure_with_manager_commit_trips(tmp_path: Path) -> None:
    """Tests fail + HEAD is a tracked manager SHA → circuit breaker trips."""
    root = tmp_path / "root"
    root.mkdir()
    # Make a real git repo so git operations work.
    repo = _make_repo(tmp_path)
    # Record HEAD sha as a tracked manager commit.
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    ).stdout.strip()
    record_manager_commit(root=repo, sha=head_sha, proposal_path="/proposal.json")

    @dataclass
    class _CP:
        returncode: int
        stdout: str = ""
        stderr: str = ""

    call_log: list[list[str]] = []

    def _mock_run(cmd, **kwargs):
        if isinstance(cmd, list):
            call_log.append(cmd)
        # Test command (shell=True) → fails.
        if kwargs.get("shell"):
            return _CP(returncode=1, stdout="", stderr="3 failed")
        # git rev-parse HEAD → real sha.
        if isinstance(cmd, list) and cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return _CP(returncode=0, stdout=head_sha)
        # git log -1 --format=%s <sha>
        if isinstance(cmd, list) and cmd[:3] == ["git", "log", "-1"]:
            return _CP(returncode=0, stdout="apply(fms): test commit")
        # git checkout -b <revert-branch>
        if isinstance(cmd, list) and cmd[:3] == ["git", "checkout", "-b"]:
            return _CP(returncode=0)
        # git revert --no-edit <sha>
        if isinstance(cmd, list) and cmd[:3] == ["git", "revert", "--no-edit"]:
            return _CP(returncode=0)
        # git push
        if isinstance(cmd, list) and cmd[:2] == ["git", "push"]:
            return _CP(returncode=0)
        # gh pr create
        if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "create"]:
            return _CP(returncode=0, stdout="https://github.com/x/y/pull/99\n")
        # Default
        return _CP(returncode=0)

    with _patch("factory.manager.circuit_breaker.subprocess.run", side_effect=_mock_run):
        result = check_and_trip(root=repo, now=NOW)

    assert result is not None, "Circuit breaker should have tripped"
    assert result["regression_commit"] == head_sha
    assert result["revert_pr_number"] == 99

    # Verify circuit_breaker.json was written.
    state = get_state(root=repo)
    assert state is not None
    assert state["schema_version"] == 1
    assert state["regression_commit"] == head_sha

    # halt_until = tripped_at + 24h
    tripped_at = datetime.fromisoformat(state["tripped_at"])
    halt_until = datetime.fromisoformat(state["halt_until"])
    assert halt_until == tripped_at + timedelta(hours=24), (
        f"halt_until should be tripped_at + 24h. "
        f"tripped_at={tripped_at} halt_until={halt_until}"
    )

    # git revert was attempted.
    revert_calls = [c for c in call_log if c[:3] == ["git", "revert", "--no-edit"]]
    assert revert_calls, "git revert should have been called"

    # gh pr create was attempted.
    pr_calls = [c for c in call_log if c[:3] == ["gh", "pr", "create"]]
    assert pr_calls, "gh pr create should have been called"


# ---------------------------------------------------------------------------
# is_tripped
# ---------------------------------------------------------------------------


def test_is_tripped_true_within_halt_window(tmp_path: Path) -> None:
    """State file with halt_until in the future → is_tripped returns True."""
    root = tmp_path / "root"
    root.mkdir()
    _write_tripped_state(root, halt_until=FUTURE_HALT)

    assert is_tripped(root=root, now=NOW) is True


def test_is_tripped_false_after_halt_window(tmp_path: Path) -> None:
    """State file with halt_until in the past → is_tripped returns False."""
    root = tmp_path / "root"
    root.mkdir()
    _write_tripped_state(root, halt_until=PAST_HALT)

    assert is_tripped(root=root, now=NOW) is False


def test_is_tripped_false_when_no_state(tmp_path: Path) -> None:
    """No state file → is_tripped returns False."""
    root = tmp_path / "root"
    root.mkdir()
    assert is_tripped(root=root, now=NOW) is False


# ---------------------------------------------------------------------------
# apply_manager_proposals refuses when tripped
# ---------------------------------------------------------------------------


def test_apply_refuses_when_tripped(tmp_path: Path) -> None:
    """With a tripped circuit breaker, apply_manager_proposals returns halted_by_circuit_breaker."""
    root = tmp_path / "root"
    root.mkdir()
    _write_tripped_state(root, halt_until=FUTURE_HALT)

    # Plant a proposal that would otherwise be processed.
    proposals_dir = root / "state" / "manager_proposals"
    proposals_dir.mkdir(parents=True)
    proposal = {
        "schema_version": 1,
        "concern_title": "test",
        "proposal": {
            "kind": "prompt_edit",
            "suggested_patch": "diff --git a/factory/personas/sm.md b/factory/personas/sm.md\n--- a/factory/personas/sm.md\n+++ b/factory/personas/sm.md\n@@ -1,2 +1,3 @@\n # SM\n body\n+new line\n",
        },
        "target_class": "prompt_edit",
        "escalate_to_human": False,
    }
    (proposals_dir / "20260526T120000-test.json").write_text(json.dumps(proposal), encoding="utf-8")

    result = apply_manager_proposals(root=root)

    assert result.get("halted_by_circuit_breaker") is True
    assert result.get("halt_until") == FUTURE_HALT
    assert result.get("processed", 0) == 0


# ---------------------------------------------------------------------------
# reset archives to history
# ---------------------------------------------------------------------------


def test_reset_archives_to_history(tmp_path: Path) -> None:
    """reset() archives the state to .circuit_breaker_history.json."""
    root = tmp_path / "root"
    root.mkdir()
    sha = "cafebabe"
    _write_tripped_state(root, halt_until=FUTURE_HALT, sha=sha)

    archived = reset(root=root, cleared_by="operator", reason="test reset")

    # circuit_breaker.json should be gone.
    assert not (root / "state" / "circuit_breaker.json").exists()
    assert get_state(root=root) is None

    # History file should exist.
    hist_path = root / "state" / ".circuit_breaker_history.json"
    assert hist_path.exists()
    history = json.loads(hist_path.read_text(encoding="utf-8"))
    assert isinstance(history, list)
    assert len(history) == 1
    entry = history[0]
    assert entry["regression_commit"] == sha
    assert entry["cleared_by"] == "operator"
    assert entry["clear_reason"] == "test reset"
    assert "cleared_at" in entry

    # Returned value matches archived entry.
    assert archived["regression_commit"] == sha
    assert archived["cleared_by"] == "operator"


def test_reset_raises_when_no_state(tmp_path: Path) -> None:
    """reset() raises FileNotFoundError if not tripped."""
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        reset(root=root)


# ---------------------------------------------------------------------------
# Classification tests (Phase 8 additions)
# ---------------------------------------------------------------------------


def _make_proposal_for_path(path: str, *, is_new_file: bool = False) -> dict[str, Any]:
    """Build a minimal proposal whose patch touches exactly one file."""
    if is_new_file:
        patch = (
            f"diff --git a/{path} b/{path}\n"
            f"--- /dev/null\n"
            f"+++ b/{path}\n"
            "@@ -0,0 +1,3 @@\n"
            "+\"\"\"New detector.\"\"\"\n"
            "+\n"
            "+def check(): return []\n"
        )
    else:
        patch = (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            "@@ -1,2 +1,3 @@\n"
            " existing line\n"
            "+new line\n"
        )
    return {
        "schema_version": 1,
        "concern_title": "test",
        "proposal": {
            "kind": "prompt_edit",
            "suggested_patch": patch,
        },
        "target_class": "prompt_edit",
        "escalate_to_human": False,
    }


def test_classify_manager_persona_edit_is_risky(tmp_path: Path) -> None:
    """A proposal touching factory/personas/manager_watcher.md → risky."""
    proposal = _make_proposal_for_path("factory/personas/manager_watcher.md")
    result = _classify_manager_proposal(proposal, tmp_path)
    assert result == "risky", (
        f"manager persona edit should be risky, got {result!r}"
    )


def test_classify_manager_persona_edit_summarizer_is_risky(tmp_path: Path) -> None:
    """factory/personas/manager_summarizer.md → risky."""
    proposal = _make_proposal_for_path("factory/personas/manager_summarizer.md")
    result = _classify_manager_proposal(proposal, tmp_path)
    assert result == "risky", f"Expected risky, got {result!r}"


def test_classify_manager_subdir_modify_is_forbidden(tmp_path: Path) -> None:
    """Modifying an existing factory/manager/detectors/cost_spike.py → forbidden."""
    proposal = _make_proposal_for_path(
        "factory/manager/detectors/cost_spike.py", is_new_file=False
    )
    result = _classify_manager_proposal(proposal, tmp_path)
    assert result == "forbidden", (
        f"Modifying existing manager subdir file should be forbidden, got {result!r}"
    )


def test_classify_new_detector_file_still_safe_after_subdir_lockdown(tmp_path: Path) -> None:
    """Adding a NEW factory/manager/detectors/new_check.py → should pass to detector_tool validator.

    The carve-out allows new detector files.  A new pure-function detector
    that passes _validate_detector_tool should be classified 'safe'.
    """
    # A new detector file that compiles cleanly.
    path = "factory/manager/detectors/new_check.py"
    patch = (
        f"diff --git a/{path} b/{path}\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1,3 @@\n"
        '+"""New detector — detects something.\"\"\"\n'
        "+\n"
        "+\n"
        "+def new_check(root):\n"
        "+    return []\n"
    )
    proposal = {
        "schema_version": 1,
        "concern_title": "test",
        "proposal": {
            "kind": "detector_tool",
            "suggested_patch": patch,
        },
        "target_class": "detector_tool",
        "escalate_to_human": False,
    }
    # The detector_tool validator will call py_compile on the new file.
    # Since it's a simple valid Python file, it should compile fine and be safe.
    result = _classify_manager_proposal(proposal, tmp_path)
    assert result == "safe", (
        f"New detector file should be safe (carve-out), got {result!r}"
    )


def test_classify_halt_module_is_forbidden(tmp_path: Path) -> None:
    """A proposal patching factory/manager/halt.py → forbidden."""
    proposal = _make_proposal_for_path(
        "factory/manager/halt.py", is_new_file=False
    )
    result = _classify_manager_proposal(proposal, tmp_path)
    assert result == "forbidden", (
        f"factory/manager/halt.py must be forbidden, got {result!r}"
    )


def test_classify_manager_apply_is_forbidden(tmp_path: Path) -> None:
    """factory/manager/apply.py → forbidden."""
    proposal = _make_proposal_for_path("factory/manager/apply.py", is_new_file=False)
    result = _classify_manager_proposal(proposal, tmp_path)
    assert result == "forbidden", f"Expected forbidden, got {result!r}"


def test_classify_manager_diagnostician_is_forbidden(tmp_path: Path) -> None:
    """factory/manager/diagnostician.py → forbidden (any depth, Phase 8 extension)."""
    proposal = _make_proposal_for_path(
        "factory/manager/diagnostician.py", is_new_file=False
    )
    result = _classify_manager_proposal(proposal, tmp_path)
    assert result == "forbidden", f"Expected forbidden, got {result!r}"
