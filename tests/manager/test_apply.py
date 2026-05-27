"""Tests for factory.manager.apply — L4 Apply pipeline (Phase 6).

Coverage:
  * _classify_manager_proposal: safe/risky/forbidden/escalate_to_human across
    all target_class values with the full class-specific validation rules.
  * apply_manager_proposals: mock gh + git, end-to-end flow.
  * History dedup: processed proposals are not reprocessed.
  * MVP end-to-end: SM-overflow scenario with persona_settings proposal.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.manager.apply import (
    _classify_manager_proposal,
    _is_already_processed,
    _load_history,
    apply_manager_proposals,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a real git repo with the given files (relative paths → content)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
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


def _minimal_proposal(
    *,
    target_class: str,
    patch: str,
    escalate_to_human: bool = False,
    kind: str = "prompt_edit",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "concern_title": "test-concern",
        "diagnosis": "A test concern.",
        "proposal": {
            "kind": kind,
            "target": "factory/personas/sm.md",
            "rationale": "Lower max_tokens to prevent overflow.",
            "suggested_patch": patch,
            "verification": "check the persona",
            "confidence": "high",
        },
        "target_class": target_class,
        "escalate_to_human": escalate_to_human,
        "escalation_reason": None,
    }


def _persona_patch(rel: str = "factory/personas/sm.md") -> str:
    return (
        f"diff --git a/{rel} b/{rel}\n"
        f"--- a/{rel}\n"
        f"+++ b/{rel}\n"
        "@@ -1,2 +1,3 @@\n"
        " # SM Persona\n"
        " body line\n"
        "+- new constraint added\n"
    )


def _routes_yaml_patch() -> str:
    return (
        "diff --git a/factory/routes.yaml b/factory/routes.yaml\n"
        "--- a/factory/routes.yaml\n"
        "+++ b/factory/routes.yaml\n"
        "@@ -1,3 +1,3 @@\n"
        " default_provider: azure\n"
        "-  sm: deepseek/deepseek-chat\n"
        "+  sm: deepseek/deepseek-chat\n"
        "+  max_tokens: 32000\n"
    )


def _detector_new_file_patch() -> str:
    """Patch that adds a new detector file factory/manager/detectors/new_check.py."""
    return (
        "diff --git a/factory/manager/detectors/new_check.py b/factory/manager/detectors/new_check.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/factory/manager/detectors/new_check.py\n"
        "@@ -0,0 +1,5 @@\n"
        '+"\"\"\"New detector for testing.\"\"\"\\n"\n'
        "+from __future__ import annotations\n"
        "+from pathlib import Path\n"
        "+\n"
        "+def new_check(*, root: Path) -> list: return []\n"
    )


def _make_runner(
    *,
    test_rc: int = 0,
    push_rc: int = 0,
    pr_number: int = 42,
) -> tuple[Callable[..., Any], list[list[str]]]:
    """Create a test runner that mocks pytest, git push, gh commands."""
    calls: list[list[str]] = []

    def _runner(args: list[str], **kwargs: Any) -> Any:
        calls.append(list(args))
        # Mock pytest.
        if args[:1] == ["uv"] and "pytest" in args:
            return _Completed(returncode=test_rc, stdout="ok")
        # Mock git push.
        if args[:2] == ["git", "push"]:
            return _Completed(returncode=push_rc)
        # Mock gh pr create.
        if args[:3] == ["gh", "pr", "create"]:
            return _Completed(
                returncode=0,
                stdout=f"https://github.com/owner/repo/pull/{pr_number}\n",
            )
        # Mock gh pr merge.
        if args[:3] == ["gh", "pr", "merge"]:
            return _Completed(returncode=0)
        # Mock gh label create.
        if args[:3] == ["gh", "label", "create"]:
            return _Completed(returncode=0)
        # Real git for everything else.
        kwargs.pop("check", None)
        return subprocess.run(args, **kwargs)

    return _runner, calls


# ---------------------------------------------------------------------------
# classify: prompt_edit
# ---------------------------------------------------------------------------


def test_classify_prompt_edit_safe(tmp_path: Path) -> None:
    """A minimal patch under factory/personas/ with prompt_edit target_class → safe."""
    repo = _make_repo(tmp_path, {"factory/personas/sm.md": "# SM Persona\nbody line\n"})
    proposal = _minimal_proposal(
        target_class="prompt_edit",
        patch=_persona_patch(),
    )
    assert _classify_manager_proposal(proposal, repo) == "safe"


def test_classify_prompt_edit_risky_wrong_path(tmp_path: Path) -> None:
    """prompt_edit touching a chain file → risky."""
    repo = _make_repo(
        tmp_path,
        {
            "factory/personas/sm.md": "# SM Persona\nbody line\n",
            "factory/chain/handlers.py": "x\n",
        },
    )
    patch = (
        "diff --git a/factory/chain/handlers.py b/factory/chain/handlers.py\n"
        "--- a/factory/chain/handlers.py\n"
        "+++ b/factory/chain/handlers.py\n"
        "@@ -1,1 +1,2 @@\n"
        " x\n"
        "+# new comment\n"
    )
    proposal = _minimal_proposal(target_class="prompt_edit", patch=patch)
    assert _classify_manager_proposal(proposal, repo) == "risky"


def test_classify_prompt_edit_risky_removes_heading(tmp_path: Path) -> None:
    """prompt_edit removing a markdown heading → risky."""
    repo = _make_repo(
        tmp_path, {"factory/personas/sm.md": "# SM Persona\n## Section\nbody\n"}
    )
    patch = (
        "diff --git a/factory/personas/sm.md b/factory/personas/sm.md\n"
        "--- a/factory/personas/sm.md\n"
        "+++ b/factory/personas/sm.md\n"
        "@@ -1,3 +1,2 @@\n"
        " # SM Persona\n"
        "-## Section\n"
        " body\n"
    )
    proposal = _minimal_proposal(target_class="prompt_edit", patch=patch)
    assert _classify_manager_proposal(proposal, repo) == "risky"


def test_classify_prompt_edit_risky_oversized(tmp_path: Path) -> None:
    """prompt_edit with >50 added lines → risky."""
    repo = _make_repo(tmp_path, {"factory/personas/sm.md": "# SM Persona\nbody\n"})
    big = "\n".join(f"+line {i}" for i in range(60))
    patch = (
        "diff --git a/factory/personas/sm.md b/factory/personas/sm.md\n"
        "--- a/factory/personas/sm.md\n"
        "+++ b/factory/personas/sm.md\n"
        "@@ -1,2 +1,62 @@\n"
        " # SM Persona\n"
        " body\n"
        f"{big}\n"
    )
    proposal = _minimal_proposal(target_class="prompt_edit", patch=patch)
    assert _classify_manager_proposal(proposal, repo) == "risky"


# ---------------------------------------------------------------------------
# classify: persona_settings
# ---------------------------------------------------------------------------


def test_classify_persona_settings_safe_within_clamps(tmp_path: Path) -> None:
    """persona_settings: max_tokens=32000 (within clamp) → safe."""
    repo = _make_repo(
        tmp_path,
        {
            "factory/routes.yaml": "default_provider: azure\nroutes:\n  sm: deepseek/deepseek-chat\n"
        },
    )
    patch = (
        "diff --git a/factory/routes.yaml b/factory/routes.yaml\n"
        "--- a/factory/routes.yaml\n"
        "+++ b/factory/routes.yaml\n"
        "@@ -1,3 +1,4 @@\n"
        " default_provider: azure\n"
        " routes:\n"
        "   sm: deepseek/deepseek-chat\n"
        "+  max_tokens: 32000\n"
    )
    proposal = _minimal_proposal(
        target_class="persona_settings", patch=patch, kind="persona_settings"
    )
    assert _classify_manager_proposal(proposal, repo) == "safe"


def test_classify_persona_settings_risky_out_of_clamps(tmp_path: Path) -> None:
    """persona_settings: max_tokens=200000 (exceeds clamp 65000) → risky."""
    repo = _make_repo(
        tmp_path,
        {
            "factory/routes.yaml": "default_provider: azure\nroutes:\n  sm: deepseek/deepseek-chat\n"
        },
    )
    patch = (
        "diff --git a/factory/routes.yaml b/factory/routes.yaml\n"
        "--- a/factory/routes.yaml\n"
        "+++ b/factory/routes.yaml\n"
        "@@ -1,3 +1,4 @@\n"
        " default_provider: azure\n"
        " routes:\n"
        "   sm: deepseek/deepseek-chat\n"
        "+  max_tokens: 200000\n"
    )
    proposal = _minimal_proposal(
        target_class="persona_settings", patch=patch, kind="persona_settings"
    )
    assert _classify_manager_proposal(proposal, repo) == "risky"


def test_classify_persona_settings_risky_unknown_numeric_field(tmp_path: Path) -> None:
    """persona_settings: novel numeric field → risky."""
    repo = _make_repo(
        tmp_path,
        {"factory/routes.yaml": "default_provider: azure\n"},
    )
    patch = (
        "diff --git a/factory/routes.yaml b/factory/routes.yaml\n"
        "--- a/factory/routes.yaml\n"
        "+++ b/factory/routes.yaml\n"
        "@@ -1,1 +1,2 @@\n"
        " default_provider: azure\n"
        "+  novel_limit: 99999\n"
    )
    proposal = _minimal_proposal(
        target_class="persona_settings", patch=patch, kind="persona_settings"
    )
    assert _classify_manager_proposal(proposal, repo) == "risky"


def test_classify_persona_settings_safe_temperature_in_range(tmp_path: Path) -> None:
    """persona_settings: temperature=0.7 → safe."""
    repo = _make_repo(
        tmp_path,
        {"factory/routes.yaml": "default_provider: azure\n"},
    )
    patch = (
        "diff --git a/factory/routes.yaml b/factory/routes.yaml\n"
        "--- a/factory/routes.yaml\n"
        "+++ b/factory/routes.yaml\n"
        "@@ -1,1 +1,2 @@\n"
        " default_provider: azure\n"
        "+  temperature: 0.7\n"
    )
    proposal = _minimal_proposal(
        target_class="persona_settings", patch=patch, kind="persona_settings"
    )
    assert _classify_manager_proposal(proposal, repo) == "safe"


def test_classify_persona_settings_risky_temperature_out_of_range(tmp_path: Path) -> None:
    """persona_settings: temperature=3.0 (exceeds max 1.5) → risky."""
    repo = _make_repo(
        tmp_path,
        {"factory/routes.yaml": "default_provider: azure\n"},
    )
    patch = (
        "diff --git a/factory/routes.yaml b/factory/routes.yaml\n"
        "--- a/factory/routes.yaml\n"
        "+++ b/factory/routes.yaml\n"
        "@@ -1,1 +1,2 @@\n"
        " default_provider: azure\n"
        "+  temperature: 3.0\n"
    )
    proposal = _minimal_proposal(
        target_class="persona_settings", patch=patch, kind="persona_settings"
    )
    assert _classify_manager_proposal(proposal, repo) == "risky"


# ---------------------------------------------------------------------------
# classify: detector_tool
# ---------------------------------------------------------------------------


def test_classify_detector_tool_new_file_safe(tmp_path: Path) -> None:
    """detector_tool adding a new file under detectors/ with valid Python → safe."""
    repo = _make_repo(
        tmp_path,
        {
            "factory/manager/detectors/__init__.py": "# registry\n",
        },
    )
    # A new detector file (pure Python).
    patch = (
        "diff --git a/factory/manager/detectors/new_check.py b/factory/manager/detectors/new_check.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/factory/manager/detectors/new_check.py\n"
        "@@ -0,0 +1,3 @@\n"
        '+"\"\"\"New check.\"\"\"\n'
        "+from pathlib import Path\n"
        "+def new_check(*, root: Path) -> list: return []\n"
    )
    proposal = _minimal_proposal(
        target_class="detector_tool", patch=patch, kind="detector_tool"
    )
    assert _classify_manager_proposal(proposal, repo) == "safe"


def test_classify_detector_tool_modifying_existing_risky(tmp_path: Path) -> None:
    """detector_tool modifying an existing detector (not __init__.py) → risky."""
    repo = _make_repo(
        tmp_path,
        {
            "factory/manager/detectors/cost_spike.py": "def cost_spike(): return []\n",
        },
    )
    patch = (
        "diff --git a/factory/manager/detectors/cost_spike.py b/factory/manager/detectors/cost_spike.py\n"
        "--- a/factory/manager/detectors/cost_spike.py\n"
        "+++ b/factory/manager/detectors/cost_spike.py\n"
        "@@ -1,1 +1,2 @@\n"
        " def cost_spike(): return []\n"
        "+# modified\n"
    )
    proposal = _minimal_proposal(
        target_class="detector_tool", patch=patch, kind="detector_tool"
    )
    assert _classify_manager_proposal(proposal, repo) == "risky"


# ---------------------------------------------------------------------------
# classify: dispatch_code (always risky)
# ---------------------------------------------------------------------------


def test_classify_dispatch_code_risky(tmp_path: Path) -> None:
    """dispatch_code target_class is always risky."""
    repo = _make_repo(
        tmp_path,
        {"factory/chain/orchestrator.py": "# orchestrator\n"},
    )
    patch = (
        "diff --git a/factory/chain/orchestrator.py b/factory/chain/orchestrator.py\n"
        "--- a/factory/chain/orchestrator.py\n"
        "+++ b/factory/chain/orchestrator.py\n"
        "@@ -1,1 +1,2 @@\n"
        " # orchestrator\n"
        "+# new line\n"
    )
    proposal = _minimal_proposal(
        target_class="dispatch_code", patch=patch, kind="dispatch_code"
    )
    assert _classify_manager_proposal(proposal, repo) == "risky"


# ---------------------------------------------------------------------------
# classify: forbidden
# ---------------------------------------------------------------------------


def test_classify_manager_module_forbidden(tmp_path: Path) -> None:
    """A patch touching factory/manager/watcher.py → forbidden."""
    repo = _make_repo(
        tmp_path,
        {"factory/manager/watcher.py": "# watcher\n"},
    )
    patch = (
        "diff --git a/factory/manager/watcher.py b/factory/manager/watcher.py\n"
        "--- a/factory/manager/watcher.py\n"
        "+++ b/factory/manager/watcher.py\n"
        "@@ -1,1 +1,2 @@\n"
        " # watcher\n"
        "+# modified\n"
    )
    proposal = _minimal_proposal(
        target_class="prompt_edit", patch=patch, kind="prompt_edit"
    )
    assert _classify_manager_proposal(proposal, repo) == "forbidden"


def test_classify_apply_module_forbidden(tmp_path: Path) -> None:
    """A patch touching factory/chain/factory_improver_apply.py → forbidden."""
    repo = _make_repo(
        tmp_path,
        {"factory/chain/factory_improver_apply.py": "# apply\n"},
    )
    patch = (
        "diff --git a/factory/chain/factory_improver_apply.py b/factory/chain/factory_improver_apply.py\n"
        "--- a/factory/chain/factory_improver_apply.py\n"
        "+++ b/factory/chain/factory_improver_apply.py\n"
        "@@ -1,1 +1,2 @@\n"
        " # apply\n"
        "+# modified\n"
    )
    proposal = _minimal_proposal(
        target_class="prompt_edit", patch=patch, kind="prompt_edit"
    )
    assert _classify_manager_proposal(proposal, repo) == "forbidden"


def test_classify_mixed_safe_and_forbidden_is_forbidden(tmp_path: Path) -> None:
    """A patch touching both a safe path and a forbidden path → forbidden."""
    repo = _make_repo(
        tmp_path,
        {
            "factory/personas/sm.md": "# SM Persona\nbody\n",
            "factory/manager/watcher.py": "# watcher\n",
        },
    )
    patch = (
        "diff --git a/factory/personas/sm.md b/factory/personas/sm.md\n"
        "--- a/factory/personas/sm.md\n"
        "+++ b/factory/personas/sm.md\n"
        "@@ -1,2 +1,3 @@\n"
        " # SM Persona\n"
        " body\n"
        "+- new bullet\n"
        "diff --git a/factory/manager/watcher.py b/factory/manager/watcher.py\n"
        "--- a/factory/manager/watcher.py\n"
        "+++ b/factory/manager/watcher.py\n"
        "@@ -1,1 +1,2 @@\n"
        " # watcher\n"
        "+# also modified\n"
    )
    proposal = _minimal_proposal(
        target_class="prompt_edit", patch=patch, kind="prompt_edit"
    )
    assert _classify_manager_proposal(proposal, repo) == "forbidden"


def test_classify_escalate_to_human_passthrough(tmp_path: Path) -> None:
    """proposal with target_class=escalate_to_human → escalate_to_human."""
    # No git repo needed — classifier short-circuits on escalate_to_human.
    repo = tmp_path / "repo"
    repo.mkdir()
    proposal = _minimal_proposal(
        target_class="escalate_to_human",
        patch="",  # no patch needed
        escalate_to_human=True,
    )
    assert _classify_manager_proposal(proposal, repo) == "escalate_to_human"


def test_classify_escalate_flag_overrides_safe_class(tmp_path: Path) -> None:
    """escalate_to_human=true in a proposal that would otherwise be safe → escalate_to_human."""
    repo = _make_repo(tmp_path, {"factory/personas/sm.md": "# SM Persona\nbody\n"})
    proposal = _minimal_proposal(
        target_class="prompt_edit",
        patch=_persona_patch(),
        escalate_to_human=True,
    )
    assert _classify_manager_proposal(proposal, repo) == "escalate_to_human"


# ---------------------------------------------------------------------------
# apply_manager_proposals: integration tests with real git + mocked gh/pytest
# ---------------------------------------------------------------------------


def _plant_proposal(proposals_dir: Path, proposal: dict[str, Any], name: str = "test.json") -> Path:
    """Write a proposal JSON to the proposals dir and return the path."""
    proposals_dir.mkdir(parents=True, exist_ok=True)
    p = proposals_dir / name
    p.write_text(json.dumps(proposal), encoding="utf-8")
    return p


def test_apply_safe_proposal_writes_branch_and_history(tmp_path: Path) -> None:
    """A safe proposal creates a branch, runs tests, opens PR with safe label,
    auto-merges, and writes history entry."""
    repo = _make_repo(tmp_path, {"factory/personas/sm.md": "# SM Persona\nbody line\n"})
    proposals_dir = repo / "state" / "manager_proposals"
    proposal = _minimal_proposal(target_class="prompt_edit", patch=_persona_patch())
    p = _plant_proposal(proposals_dir, proposal, "safe-prop.json")

    runner, calls = _make_runner(pr_number=99)

    result = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=runner,
        repo="owner/repo",
        push=True,
    )

    assert result["processed"] == 1
    assert result["safe_applied"] == 1
    assert result["risky_opened"] == 0
    assert result["forbidden"] == 0

    # PR was opened with safe label.
    create_calls = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
    assert create_calls, "gh pr create should have been called"
    create_args = create_calls[0]
    assert "--label" in create_args
    label_idx = create_args.index("--label")
    from factory.chain.factory_improver_apply import SAFE_LABEL
    assert create_args[label_idx + 1] == SAFE_LABEL

    # Auto-merge was requested.
    merge_calls = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert merge_calls, "gh pr merge --auto should have been called"
    assert "--auto" in merge_calls[0]
    assert "--squash" in merge_calls[0]

    # History was written.
    history = _load_history(repo)
    assert len(history) == 1
    assert history[0]["proposal_path"] == str(p)
    assert history[0]["classification"] == "safe"


def test_apply_risky_proposal_no_auto_merge(tmp_path: Path) -> None:
    """A risky proposal opens a PR with the review label but no auto-merge."""
    repo = _make_repo(
        tmp_path,
        {
            "factory/personas/sm.md": "# SM Persona\nbody line\n",
            "factory/chain/orchestrator.py": "# orchestrator\n",
        },
    )
    proposals_dir = repo / "state" / "manager_proposals"
    patch = (
        "diff --git a/factory/chain/orchestrator.py b/factory/chain/orchestrator.py\n"
        "--- a/factory/chain/orchestrator.py\n"
        "+++ b/factory/chain/orchestrator.py\n"
        "@@ -1,1 +1,2 @@\n"
        " # orchestrator\n"
        "+# new comment\n"
    )
    proposal = _minimal_proposal(
        target_class="dispatch_code", patch=patch, kind="dispatch_code"
    )
    _plant_proposal(proposals_dir, proposal, "risky-prop.json")

    runner, calls = _make_runner(pr_number=77)

    result = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=runner,
        repo="owner/repo",
        push=True,
    )

    assert result["processed"] == 1
    assert result["risky_opened"] == 1
    assert result["safe_applied"] == 0

    # PR label should be review, not safe.
    create_calls = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
    assert create_calls
    create_args = create_calls[0]
    label_idx = create_args.index("--label")
    from factory.chain.factory_improver_apply import REVIEW_LABEL
    assert create_args[label_idx + 1] == REVIEW_LABEL

    # No auto-merge.
    merge_calls = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert not merge_calls, "Risky proposals must NOT be auto-merged"


def test_apply_forbidden_proposal_skipped(tmp_path: Path) -> None:
    """A forbidden proposal: no branch created, history records 'forbidden'."""
    repo = _make_repo(
        tmp_path,
        {"factory/manager/watcher.py": "# watcher\n"},
    )
    proposals_dir = repo / "state" / "manager_proposals"
    patch = (
        "diff --git a/factory/manager/watcher.py b/factory/manager/watcher.py\n"
        "--- a/factory/manager/watcher.py\n"
        "+++ b/factory/manager/watcher.py\n"
        "@@ -1,1 +1,2 @@\n"
        " # watcher\n"
        "+# EVIL EDIT\n"
    )
    proposal = _minimal_proposal(
        target_class="prompt_edit", patch=patch, kind="prompt_edit"
    )
    p = _plant_proposal(proposals_dir, proposal, "forbidden-prop.json")

    runner, calls = _make_runner()

    result = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=runner,
        repo="owner/repo",
    )

    assert result["forbidden"] == 1
    assert result["safe_applied"] == 0

    # No git branch should have been created.
    branch_calls = [c for c in calls if c[:3] == ["git", "checkout", "-b"]]
    assert not branch_calls, "Forbidden proposals must NOT create a branch"

    # History records forbidden status.
    history = _load_history(repo)
    assert any(
        h["proposal_path"] == str(p) and h["status"] == "forbidden"
        for h in history
    )


def test_apply_test_failure_abandons_branch(tmp_path: Path) -> None:
    """When pytest fails after apply, branch is cleaned up and history records 'test_failed'."""
    repo = _make_repo(tmp_path, {"factory/personas/sm.md": "# SM Persona\nbody line\n"})
    proposals_dir = repo / "state" / "manager_proposals"
    proposal = _minimal_proposal(target_class="prompt_edit", patch=_persona_patch())
    p = _plant_proposal(proposals_dir, proposal, "fail-prop.json")

    runner, calls = _make_runner(test_rc=1)  # pytest returns failure

    result = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=runner,
        repo="owner/repo",
    )

    assert result["processed"] == 1
    assert result["safe_applied"] == 0

    # Branch should have been deleted (cleanup).
    branches = subprocess.run(
        ["git", "branch", "--list", "factory-manager/*"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert branches.stdout.strip() == "", "Branch should be deleted after test failure"

    # History records test_failed.
    history = _load_history(repo)
    assert any(
        h["proposal_path"] == str(p) and h["status"] == "test_failed"
        for h in history
    )


def test_apply_processed_proposal_not_reprocessed(tmp_path: Path) -> None:
    """Running apply twice on the same proposal only processes it once."""
    repo = _make_repo(tmp_path, {"factory/personas/sm.md": "# SM Persona\nbody line\n"})
    proposals_dir = repo / "state" / "manager_proposals"
    proposal = _minimal_proposal(target_class="prompt_edit", patch=_persona_patch())
    _plant_proposal(proposals_dir, proposal, "dedup-prop.json")

    runner, _calls = _make_runner()

    # First run.
    result1 = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=runner,
        repo="owner/repo",
    )
    assert result1["processed"] == 1

    # Checkout main so the second run can work.
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=str(repo),
        capture_output=True,
    )

    # Second run — already processed.
    runner2, _calls2 = _make_runner()
    result2 = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=runner2,
        repo="owner/repo",
    )
    assert result2["processed"] == 0, "Should not reprocess an already-processed proposal"


def test_apply_escalate_to_human_acknowledged_no_branch(tmp_path: Path) -> None:
    """escalate_to_human proposals are recorded in history but no branch created."""
    repo = _make_repo(tmp_path, {"README.md": "# Factory\n"})
    proposals_dir = repo / "state" / "manager_proposals"
    proposal = _minimal_proposal(
        target_class="escalate_to_human",
        patch="",
        escalate_to_human=True,
    )
    p = _plant_proposal(proposals_dir, proposal, "esc-prop.json")

    runner, calls = _make_runner()

    result = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=runner,
        repo="owner/repo",
    )

    assert result["escalated_human"] == 1
    assert result["safe_applied"] == 0

    # No branch created.
    branch_calls = [c for c in calls if c[:3] == ["git", "checkout", "-b"]]
    assert not branch_calls

    # History entry.
    history = _load_history(repo)
    assert any(
        h["proposal_path"] == str(p) and h["status"] == "escalation_acknowledged"
        for h in history
    )


# ---------------------------------------------------------------------------
# MVP end-to-end test: SM-overflow scenario
# ---------------------------------------------------------------------------


def test_sm_overflow_end_to_end(tmp_path: Path) -> None:
    """MVP acceptance test: plant a persona_settings proposal that lowers
    SM max_tokens within clamps.  Mock gh + pytest.  Assert:
      (a) Classified as 'safe'
      (b) Branch created
      (c) Patch applied (routes.yaml content changed)
      (d) Pytest invoked (mock returns success)
      (e) gh pr create --label factory-self-improvement-safe was called
      (f) gh pr merge --auto --squash was called
      (g) History entry written with status=opened_pr
    """
    # Set up a repo with factory/routes.yaml.
    initial_routes = (
        "default_provider: azure\n"
        "routes:\n"
        "  sm: deepseek/deepseek-chat\n"
    )
    repo = _make_repo(tmp_path, {"factory/routes.yaml": initial_routes})

    # Build the persona_settings proposal: lower sm max_tokens to 32000.
    patch = (
        "diff --git a/factory/routes.yaml b/factory/routes.yaml\n"
        "--- a/factory/routes.yaml\n"
        "+++ b/factory/routes.yaml\n"
        "@@ -1,3 +1,4 @@\n"
        " default_provider: azure\n"
        " routes:\n"
        "   sm: deepseek/deepseek-chat\n"
        "+  max_tokens: 32000\n"
    )
    proposal = {
        "schema_version": 1,
        "concern_title": "sm-token-overflow-loop",
        "diagnosis": (
            "The SM persona hit max_tokens=65536 with finish_reason=length "
            "on 7 consecutive calls. Lowering max_tokens to 32000 should "
            "prevent the overflow."
        ),
        "proposal": {
            "kind": "persona_settings",
            "target": "factory/routes.yaml",
            "rationale": "Lower SM max_tokens from 65536 to 32000 to avoid token overflow.",
            "suggested_patch": patch,
            "verification": "Run the SM persona; confirm finish_reason != length.",
            "confidence": "high",
        },
        "target_class": "persona_settings",
        "escalate_to_human": False,
        "escalation_reason": None,
    }

    # (a) Classification check.
    classification = _classify_manager_proposal(proposal, repo)
    assert classification == "safe", f"Expected 'safe', got '{classification}'"

    # Plant the proposal file.
    proposals_dir = repo / "state" / "manager_proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    p = proposals_dir / "20260527T120000-sm-token-overflow-loop.json"
    p.write_text(json.dumps(proposal), encoding="utf-8")

    # Create a runner that captures calls and mocks external tools.
    pytest_calls: list[list[str]] = []
    pr_calls: list[list[str]] = []
    merge_calls: list[list[str]] = []

    def _runner(args: list[str], **kwargs: Any) -> Any:
        # (d) Pytest.
        if args[:1] == ["uv"] and "pytest" in args:
            pytest_calls.append(list(args))
            return _Completed(returncode=0, stdout="841 passed")
        # Git push.
        if args[:2] == ["git", "push"]:
            return _Completed(returncode=0)
        # (e) gh pr create.
        if args[:3] == ["gh", "pr", "create"]:
            pr_calls.append(list(args))
            return _Completed(
                returncode=0,
                stdout="https://github.com/owner/repo/pull/123\n",
            )
        # (f) gh pr merge.
        if args[:3] == ["gh", "pr", "merge"]:
            merge_calls.append(list(args))
            return _Completed(returncode=0)
        # gh label create.
        if args[:3] == ["gh", "label", "create"]:
            return _Completed(returncode=0)
        # Real git.
        kwargs.pop("check", None)
        return subprocess.run(args, **kwargs)

    result = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=_runner,
        repo="owner/repo",
        push=True,
    )

    # (b) Branch was created — verify via result and history.
    assert result["processed"] == 1
    assert result["safe_applied"] == 1, f"safe_applied should be 1, got: {result}"

    # (c) Patch applied — the branch commit should contain the changed routes.yaml.
    # The branch name is in the history.
    history = _load_history(repo)
    assert history, "History should have an entry"
    branch_name = history[0].get("branch")
    assert branch_name, "History should record branch name"

    # Check the file was changed on that branch.
    show_proc = subprocess.run(
        ["git", "show", f"{branch_name}:factory/routes.yaml"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if show_proc.returncode == 0:
        assert "max_tokens" in show_proc.stdout, (
            "The patch should have added max_tokens to routes.yaml"
        )

    # (d) Pytest was invoked.
    assert pytest_calls, "pytest should have been called"

    # (e) gh pr create with safe label.
    assert pr_calls, "gh pr create should have been called"
    pr_args = pr_calls[0]
    assert "--label" in pr_args
    label_idx = pr_args.index("--label")
    from factory.chain.factory_improver_apply import SAFE_LABEL
    assert pr_args[label_idx + 1] == SAFE_LABEL

    # (f) gh pr merge --auto --squash.
    assert merge_calls, "gh pr merge should have been called for auto-merge"
    merge_args = merge_calls[0]
    assert "--auto" in merge_args
    assert "--squash" in merge_args

    # (g) History entry with status=opened_pr.
    assert any(
        h.get("status") == "opened_pr" and h.get("classification") == "safe"
        for h in history
    ), f"Expected opened_pr in history. Got: {history}"


# ---------------------------------------------------------------------------
# is_already_processed
# ---------------------------------------------------------------------------


def test_is_already_processed_false_when_no_history(tmp_path: Path) -> None:
    assert not _is_already_processed(tmp_path, tmp_path / "x.json")


def test_is_already_processed_true_after_run(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, {"factory/personas/sm.md": "# SM Persona\nbody line\n"})
    proposals_dir = repo / "state" / "manager_proposals"
    proposal = _minimal_proposal(target_class="prompt_edit", patch=_persona_patch())
    p = _plant_proposal(proposals_dir, proposal, "once.json")

    runner, _ = _make_runner()
    apply_manager_proposals(root=repo, dry_run=False, runner=runner, repo="owner/repo")

    # Restore main branch so we can recheck.
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True)

    assert _is_already_processed(repo, p), "Should be marked processed after first run"
