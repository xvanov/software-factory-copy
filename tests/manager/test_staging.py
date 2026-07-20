"""Tests for factory.manager.staging — canary/shadow deploy for self-edits.

Coverage:
  * is_self_edit / self_edit_paths: factory/ vs app-repo path detection.
  * sync_copy_from_main: git command construction (mocked runner).
  * stage_and_validate_self_edit: healthy → StagingResult(healthy=True);
    a failing validation stage → healthy=False + stage_failed; a git-plumbing
    failure (clone/push) → healthy=False; sync/clone infra failure → raises
    StagingInfraError.
  * gate_self_edit fail-safe: healthy → promote; unhealthy → not promoted;
    infra error → not promoted (never raises); events emitted.
  * apply.py wiring: a self-edit routes through staging (healthy → promotes,
    unhealthy/infra → NOT promoted + no real-factory branch); an app-repo
    change bypasses staging; a forbidden proposal is still blocked.

All heavy subprocess work (nested uv sync / pytest / clone) is MOCKED — the
suite never actually runs a nested factory.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from factory.manager import staging as staging_mod
from factory.manager.apply import apply_manager_proposals
from factory.manager.staging import (
    StagingInfraError,
    StagingResult,
    _default_validator,
    gate_self_edit,
    is_self_edit,
    self_edit_paths,
    stage_and_validate_self_edit,
    sync_copy_from_main,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _self_edit_patch(rel: str = "factory/personas/sm.md") -> str:
    return (
        f"diff --git a/{rel} b/{rel}\n"
        f"--- a/{rel}\n"
        f"+++ b/{rel}\n"
        "@@ -1,2 +1,3 @@\n"
        " # SM Persona\n"
        " body line\n"
        "+new line\n"
    )


def _app_patch(rel: str = "apps/sacrifice/README.md") -> str:
    return (
        f"diff --git a/{rel} b/{rel}\n"
        f"--- a/{rel}\n"
        f"+++ b/{rel}\n"
        "@@ -1,1 +1,2 @@\n"
        " # readme\n"
        "+new line\n"
    )


def _proposal(patch: str, *, target_class: str = "prompt_edit", pid: str = "prop-1") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "proposal_id": pid,
        "concern_title": "test-concern",
        "diagnosis": "diag",
        "proposal": {
            "kind": "prompt_edit",
            "target": "factory/personas/sm.md",
            "rationale": "why",
            "suggested_patch": patch,
            "confidence": "high",
        },
        "target_class": target_class,
        "escalate_to_human": False,
    }


def _make_copy_runner(
    *,
    fail_stage: str | None = None,
    fail_rc: int = 1,
) -> tuple[Callable[..., Any], list[list[str]]]:
    """A runner that mocks ALL staging git + validation subprocess calls.

    ``fail_stage`` optionally forces one command family to return non-zero,
    so we can simulate a failing validation stage or a git-plumbing failure
    without ever touching a real network or nested venv.
    """
    calls: list[list[str]] = []

    def _runner(args: list[str], **kwargs: Any) -> Any:
        calls.append(list(args))

        def _rc(name: str) -> int:
            return fail_rc if fail_stage == name else 0

        # git fetch / push / clone / config / checkout / add / commit / apply
        if args[:2] == ["git", "fetch"]:
            return _Completed(returncode=_rc("fetch"))
        if args[:2] == ["git", "push"]:
            return _Completed(returncode=_rc("push"))
        if args[:2] == ["git", "clone"]:
            return _Completed(returncode=_rc("clone"))
        if args[:2] == ["git", "apply"]:
            return _Completed(returncode=_rc("apply"), stderr="patch failed" if fail_stage == "apply" else "")
        if args[:1] == ["git"]:
            # config / checkout / add / commit → succeed
            return _Completed(returncode=0)
        # Validation stages.
        if args[:2] == ["uv", "sync"]:
            return _Completed(returncode=_rc("uv_sync"), stdout="synced")
        if args[:1] == ["uv"] and "pytest" in args:
            return _Completed(returncode=_rc("pytest"), stdout="tests", stderr="boom" if fail_stage == "pytest" else "")
        if args[:1] == ["uv"] and args[1:3] == ["run", "factory"] and "--help" in args:
            return _Completed(returncode=_rc("import_smoke"), stdout="usage")
        if args[:1] == ["uv"] and "tick" in args:
            return _Completed(returncode=_rc("dry_run_tick"), stdout="tick ok")
        return _Completed(returncode=0)

    return _runner, calls


# ---------------------------------------------------------------------------
# is_self_edit / self_edit_paths
# ---------------------------------------------------------------------------


def test_is_self_edit_true_for_factory_paths() -> None:
    assert is_self_edit(["factory/personas/sm.md"]) is True
    assert is_self_edit(["factory/routes.yaml"]) is True
    assert is_self_edit(["factory/manager/detectors/x.py"]) is True


def test_is_self_edit_false_for_app_paths() -> None:
    assert is_self_edit(["apps/sacrifice/README.md"]) is False
    assert is_self_edit(["README.md", "docs/x.md"]) is False
    assert is_self_edit([]) is False
    # A directory literally named "factory-foo" must NOT match.
    assert is_self_edit(["factory-foo/bar.py"]) is False


def test_self_edit_paths_filters() -> None:
    patch = _self_edit_patch()
    assert self_edit_paths(patch) == ["factory/personas/sm.md"]
    assert self_edit_paths(_app_patch()) == []


def _rename_into_factory_patch(dest: str = "factory/evil.py") -> str:
    """A pure 100%-similarity rename INTO factory/ from OUTSIDE factory/.

    Carries NO ``+++`` hunk header — the destination lives only on the
    ``diff --git`` line. Regression for rename-into-factory evasion.
    """
    return (
        f"diff --git a/apps/x.py b/{dest}\n"
        "similarity index 100%\n"
        "rename from apps/x.py\n"
        f"rename to {dest}\n"
    )


def test_diff_target_paths_extracts_rename_destination() -> None:
    from factory.chain.factory_improver_apply import _diff_target_paths

    paths = _diff_target_paths(_rename_into_factory_patch("factory/evil.py"))
    # BOTH the source (a/) and the rename destination (b/) must appear.
    assert "apps/x.py" in paths
    assert "factory/evil.py" in paths


def test_rename_into_factory_is_detected_as_self_edit() -> None:
    from factory.chain.factory_improver_apply import _diff_target_paths

    patch = _rename_into_factory_patch("factory/evil.py")
    assert is_self_edit(_diff_target_paths(patch)) is True


def test_rename_into_manager_is_classified_forbidden() -> None:
    from factory.manager.apply import _classify_manager_proposal

    patch = _rename_into_factory_patch("factory/manager/evil.py")
    proposal = _proposal(patch, target_class="prompt_edit", pid="rename-forbid")
    assert _classify_manager_proposal(proposal, Path(".")) == "forbidden"


# ---------------------------------------------------------------------------
# sync_copy_from_main — git command construction
# ---------------------------------------------------------------------------


def test_sync_copy_from_main_command_construction(tmp_path: Path) -> None:
    runner, calls = _make_copy_runner()
    sync_copy_from_main(root=tmp_path, copy_url="git@example:copy.git", runner=runner)

    # fetch origin main, then force-push origin/main onto copy main.
    assert calls[0] == ["git", "fetch", "origin", "main"]
    assert calls[1] == [
        "git",
        "push",
        "--force",
        "git@example:copy.git",
        "refs/remotes/origin/main:refs/heads/main",
    ]


def test_sync_copy_from_main_raises_on_fetch_failure(tmp_path: Path) -> None:
    runner, _ = _make_copy_runner(fail_stage="fetch")
    with pytest.raises(StagingInfraError, match="fetch"):
        sync_copy_from_main(root=tmp_path, copy_url="c", runner=runner)


def test_sync_copy_from_main_raises_on_push_failure(tmp_path: Path) -> None:
    runner, _ = _make_copy_runner(fail_stage="push")
    with pytest.raises(StagingInfraError, match="mirror-push"):
        sync_copy_from_main(root=tmp_path, copy_url="c", runner=runner)


# ---------------------------------------------------------------------------
# stage_and_validate_self_edit
# ---------------------------------------------------------------------------


def test_stage_healthy_when_validator_passes(tmp_path: Path) -> None:
    runner, calls = _make_copy_runner()

    def _healthy_validator(checkout_dir: Path, **_: Any) -> StagingResult:
        return StagingResult(healthy=True, logs_tail="ok")

    res = stage_and_validate_self_edit(
        _proposal(_self_edit_patch()),
        root=tmp_path,
        copy_url="c",
        runner=runner,
        validator=_healthy_validator,
    )
    assert res.healthy is True
    assert res.stage_failed is None
    assert res.branch == "staging/prop-1"
    assert res.proposal_id == "prop-1"
    # It synced, cloned, applied, pushed, and cloned again for verify.
    assert ["git", "fetch", "origin", "main"] in calls
    assert any(c[:2] == ["git", "apply"] for c in calls)
    assert sum(1 for c in calls if c[:2] == ["git", "clone"]) == 2


def test_stage_unhealthy_when_validator_fails(tmp_path: Path) -> None:
    runner, _ = _make_copy_runner()

    def _failing_validator(checkout_dir: Path, **_: Any) -> StagingResult:
        return StagingResult(healthy=False, stage_failed="pytest", logs_tail="red")

    res = stage_and_validate_self_edit(
        _proposal(_self_edit_patch()),
        root=tmp_path,
        copy_url="c",
        runner=runner,
        validator=_failing_validator,
    )
    assert res.healthy is False
    assert res.stage_failed == "pytest"
    assert res.branch == "staging/prop-1"


def test_stage_apply_failure_is_unhealthy_not_raise(tmp_path: Path) -> None:
    runner, _ = _make_copy_runner(fail_stage="apply")
    res = stage_and_validate_self_edit(
        _proposal(_self_edit_patch()),
        root=tmp_path,
        copy_url="c",
        runner=runner,
        validator=lambda *a, **k: StagingResult(healthy=True),
    )
    assert res.healthy is False
    assert res.stage_failed == "apply"


def test_stage_push_failure_never_promotes(tmp_path: Path) -> None:
    # `git push` matches both the sync mirror-push and the branch push. The
    # mirror-push inside sync runs first and fails → StagingInfraError. The key
    # property: a push failure NEVER yields a healthy/promotable result.
    runner, _ = _make_copy_runner(fail_stage="push")
    with pytest.raises(StagingInfraError):
        stage_and_validate_self_edit(
            _proposal(_self_edit_patch()),
            root=tmp_path,
            copy_url="c",
            runner=runner,
            validator=lambda *a, **k: StagingResult(healthy=True),
        )


def test_stage_clone_failure_raises_infra_error(tmp_path: Path) -> None:
    runner, _ = _make_copy_runner(fail_stage="clone")
    with pytest.raises(StagingInfraError, match="clone"):
        stage_and_validate_self_edit(
            _proposal(_self_edit_patch()),
            root=tmp_path,
            copy_url="c",
            runner=runner,
            validator=lambda *a, **k: StagingResult(healthy=True),
        )


def test_stage_no_patch_raises_infra_error(tmp_path: Path) -> None:
    runner, _ = _make_copy_runner()
    with pytest.raises(StagingInfraError, match="no suggested_patch"):
        stage_and_validate_self_edit(
            _proposal(""),
            root=tmp_path,
            copy_url="c",
            runner=runner,
        )


# ---------------------------------------------------------------------------
# _default_validator — stage order, stop-at-first-failure, timeout fail-safe
# ---------------------------------------------------------------------------


def test_default_validator_runs_all_stages_in_order(tmp_path: Path) -> None:
    runner, calls = _make_copy_runner()
    (tmp_path / "apps" / "sacrifice").mkdir(parents=True)
    res = _default_validator(tmp_path / "checkout", root=tmp_path, runner=runner)
    assert res.healthy is True
    families = [c[:3] for c in calls]
    assert ["uv", "sync", "--all-extras"] in families
    assert any(c[:1] == ["uv"] and "pytest" in c for c in calls)
    assert any("--help" in c for c in calls)
    assert any("tick" in c for c in calls)


def test_default_validator_stops_at_first_failure(tmp_path: Path) -> None:
    runner, calls = _make_copy_runner(fail_stage="uv_sync")
    res = _default_validator(tmp_path / "checkout", root=tmp_path, runner=runner)
    assert res.healthy is False
    assert res.stage_failed == "uv_sync"
    # No pytest/help/tick should have run after uv_sync failed.
    assert not any(c[:1] == ["uv"] and "pytest" in c for c in calls)


def test_default_validator_pytest_failure(tmp_path: Path) -> None:
    runner, _ = _make_copy_runner(fail_stage="pytest")
    res = _default_validator(tmp_path / "checkout", root=tmp_path, runner=runner)
    assert res.healthy is False
    assert res.stage_failed == "pytest"


def test_default_validator_timeout_is_failsafe(tmp_path: Path) -> None:
    def _timeout_runner(args: list[str], **kwargs: Any) -> Any:
        if args[:1] == ["uv"] and "pytest" in args:
            raise subprocess.TimeoutExpired(cmd=args, timeout=1)
        return _Completed(returncode=0)

    res = _default_validator(tmp_path / "checkout", root=tmp_path, runner=_timeout_runner)
    assert res.healthy is False
    assert res.stage_failed == "pytest"
    assert "TIMEOUT" in res.logs_tail


# ---------------------------------------------------------------------------
# gate_self_edit — fail-safe + events
# ---------------------------------------------------------------------------


def _read_events(root: Path, stream: str) -> list[dict[str, Any]]:
    p = root / "state" / "events" / f"{stream}.ndjson"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def test_gate_promotes_on_healthy(tmp_path: Path) -> None:
    runner, _ = _make_copy_runner()
    decision = gate_self_edit(
        _proposal(_self_edit_patch()),
        "state/manager_proposals/p.json",
        root=tmp_path,
        copy_url="c",
        runner=runner,
        validator=lambda *a, **k: StagingResult(healthy=True),
    )
    assert decision.promote is True
    assert decision.status == "staging_validated"
    events = _read_events(tmp_path, staging_mod.STAGING_STREAM)
    assert any(e["event"] == "staging_validated" and e["promoted"] is True for e in events)


def test_gate_does_not_promote_on_unhealthy(tmp_path: Path) -> None:
    runner, _ = _make_copy_runner()
    decision = gate_self_edit(
        _proposal(_self_edit_patch()),
        "state/manager_proposals/p.json",
        root=tmp_path,
        copy_url="c",
        runner=runner,
        validator=lambda *a, **k: StagingResult(healthy=False, stage_failed="dry_run_tick", logs_tail="crash"),
    )
    assert decision.promote is False
    assert decision.status == "staging_rejected"
    assert decision.stage_failed == "dry_run_tick"
    events = _read_events(tmp_path, staging_mod.STAGING_STREAM)
    assert any(e["event"] == "staging_rejected" and e["promoted"] is False for e in events)


def test_gate_infra_failure_does_not_promote_and_never_raises(tmp_path: Path) -> None:
    # fetch fails inside sync → StagingInfraError inside gate → fail-safe.
    runner, _ = _make_copy_runner(fail_stage="fetch")
    decision = gate_self_edit(
        _proposal(_self_edit_patch()),
        "state/manager_proposals/p.json",
        root=tmp_path,
        copy_url="c",
        runner=runner,
        validator=lambda *a, **k: StagingResult(healthy=True),
    )
    assert decision.promote is False
    assert decision.status == "staging_infra_failed"
    events = _read_events(tmp_path, staging_mod.STAGING_STREAM)
    assert any(e["event"] == "staging_infra_failed" and e["promoted"] is False for e in events)


# ---------------------------------------------------------------------------
# apply.py wiring
# ---------------------------------------------------------------------------


def _make_apply_runner(*, pr_number: int = 55) -> tuple[Callable[..., Any], list[list[str]]]:
    """Runner for the REAL-factory apply path (promotion)."""
    calls: list[list[str]] = []

    def _runner(args: list[str], **kwargs: Any) -> Any:
        calls.append(list(args))
        if args[:1] == ["uv"] and "pytest" in args:
            return _Completed(returncode=0, stdout="ok")
        if args[:2] == ["git", "push"]:
            return _Completed(returncode=0)
        if args[:3] == ["gh", "pr", "create"]:
            return _Completed(returncode=0, stdout=f"https://github.com/o/r/pull/{pr_number}\n")
        if args[:3] == ["gh", "pr", "merge"]:
            return _Completed(returncode=0)
        if args[:3] == ["gh", "label", "create"]:
            return _Completed(returncode=0)
        kwargs.pop("check", None)
        return subprocess.run(args, **kwargs)

    return _runner, calls


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
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


def _plant(repo: Path, proposal: dict[str, Any], name: str) -> Path:
    d = repo / "state" / "manager_proposals"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps(proposal), encoding="utf-8")
    return p


def test_apply_self_edit_healthy_promotes(tmp_path: Path) -> None:
    """A self-edit that stages healthy proceeds to the real PR/auto-merge path."""
    repo = _make_repo(tmp_path, {"factory/personas/sm.md": "# SM Persona\nbody line\n"})
    _plant(repo, _proposal(_self_edit_patch(), pid="ok-1"), "p.json")
    runner, calls = _make_apply_runner(pr_number=91)

    def _healthy_gate(proposal: dict[str, Any], proposal_path: str, **kwargs: Any) -> Any:
        from factory.manager.staging import StagingDecision

        return StagingDecision(promote=True, status="staging_validated", branch="staging/ok-1")

    result = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=runner,
        repo="owner/repo",
        push=True,
        staging_gate=_healthy_gate,
    )
    assert result["safe_applied"] == 1
    assert any(c[:3] == ["gh", "pr", "create"] for c in calls)


def test_apply_self_edit_unhealthy_not_promoted(tmp_path: Path) -> None:
    """An unhealthy self-edit is NOT promoted: no branch, no PR on real factory."""
    repo = _make_repo(tmp_path, {"factory/personas/sm.md": "# SM Persona\nbody line\n"})
    _plant(repo, _proposal(_self_edit_patch(), pid="bad-1"), "p.json")
    runner, calls = _make_apply_runner()

    def _reject_gate(proposal: dict[str, Any], proposal_path: str, **kwargs: Any) -> Any:
        from factory.manager.staging import StagingDecision

        return StagingDecision(
            promote=False, status="staging_rejected", stage_failed="pytest", logs_tail="red"
        )

    result = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=runner,
        repo="owner/repo",
        push=True,
        staging_gate=_reject_gate,
    )
    assert result["safe_applied"] == 0
    assert result["staging_rejected"] == 1
    # Real factory never touched: no gh pr create, no branch checkout.
    assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)
    branches = subprocess.run(
        ["git", "branch", "--list", "factory-manager/*"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    assert branches.stdout.strip() == ""


def test_apply_self_edit_infra_failure_not_promoted(tmp_path: Path) -> None:
    """Staging infra failure → not promoted, manager cycle continues (no raise)."""
    repo = _make_repo(tmp_path, {"factory/personas/sm.md": "# SM Persona\nbody line\n"})
    _plant(repo, _proposal(_self_edit_patch(), pid="infra-1"), "p.json")
    runner, calls = _make_apply_runner()

    def _infra_gate(proposal: dict[str, Any], proposal_path: str, **kwargs: Any) -> Any:
        from factory.manager.staging import StagingDecision

        return StagingDecision(promote=False, status="staging_infra_failed", logs_tail="unreachable")

    result = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=runner,
        repo="owner/repo",
        push=True,
        staging_gate=_infra_gate,
    )
    assert result["staging_infra_failed"] == 1
    assert result["safe_applied"] == 0
    assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)


def test_apply_app_edit_bypasses_staging(tmp_path: Path) -> None:
    """An app-repo change (not under factory/) does NOT invoke the staging gate."""
    repo = _make_repo(tmp_path, {"apps/sacrifice/README.md": "# readme\n"})
    # dispatch_code so it's classified risky (well-formed, opens a review PR).
    prop = _proposal(_app_patch(), target_class="dispatch_code", pid="app-1")
    _plant(repo, prop, "p.json")
    runner, calls = _make_apply_runner()

    gate_invoked = {"n": 0}

    def _spy_gate(*a: Any, **k: Any) -> Any:
        gate_invoked["n"] += 1
        from factory.manager.staging import StagingDecision

        return StagingDecision(promote=False, status="staging_rejected")

    result = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=runner,
        repo="owner/repo",
        push=True,
        staging_gate=_spy_gate,
    )
    assert gate_invoked["n"] == 0, "app-repo change must bypass staging"
    assert result["risky_opened"] == 1


def test_apply_forbidden_still_blocked_before_staging(tmp_path: Path) -> None:
    """A forbidden self-edit (manager/*.py) is blocked and never reaches staging."""
    repo = _make_repo(tmp_path, {"factory/manager/apply.py": "# x\n"})
    patch = (
        "diff --git a/factory/manager/apply.py b/factory/manager/apply.py\n"
        "--- a/factory/manager/apply.py\n"
        "+++ b/factory/manager/apply.py\n"
        "@@ -1,1 +1,2 @@\n"
        " # x\n"
        "+# evil\n"
    )
    _plant(repo, _proposal(patch, target_class="prompt_edit", pid="forbid-1"), "p.json")
    runner, _ = _make_apply_runner()

    gate_invoked = {"n": 0}

    def _spy_gate(*a: Any, **k: Any) -> Any:
        gate_invoked["n"] += 1
        from factory.manager.staging import StagingDecision

        return StagingDecision(promote=True, status="staging_validated")

    result = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=runner,
        repo="owner/repo",
        push=True,
        staging_gate=_spy_gate,
    )
    assert result["forbidden"] == 1
    assert gate_invoked["n"] == 0, "forbidden proposal must not reach staging"
