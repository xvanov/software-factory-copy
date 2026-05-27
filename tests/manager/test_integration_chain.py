"""Chained integration test L1 → L2 → L3 → L4 (Phase 6.1).

PRD MVP acceptance criterion #6:
    "The full test runs in CI with all LLM calls mocked via recorded fixtures,
    so the loop is testable without spending money."

This test wires up a synthetic SM max-tokens-overflow incident end-to-end:
  1. L1 Watcher reads the planted run failures and escalates to L2.
  2. L2 Summarizer reads the escalated note and produces a concern file.
  3. L3 Diagnostician reads the concern and produces a proposal with a patch.
  4. L4 Apply pipeline classifies the proposal as 'safe', creates a branch,
     applies the patch, invokes pytest (mocked), opens a PR, and auto-merges.

Cost-bound invariant: exactly 3 text_run calls total (one per L1/L2/L3).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from factory.manager.apply import _load_history, apply_manager_proposals
from factory.manager.diagnostician import run_diagnostician_once
from factory.manager.summarizer import run_summarizer_once
from factory.manager.watcher import run_watcher_once

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
T0 = NOW - timedelta(minutes=50)   # first failure
T1 = T0 + timedelta(minutes=16)    # second failure
T2 = T1 + timedelta(minutes=16)    # third failure

# Story IDs that will appear in the evidence list.
RUN_STORY_IDS = [110, 111, 112]

# ---------------------------------------------------------------------------
# The real factory/routes.yaml content — used to initialise the temp repo so
# the L3-fixture patch applies cleanly.  We copy from the live file at test
# setup time rather than hard-coding, so any in-repo changes are picked up
# automatically.
# ---------------------------------------------------------------------------

_REAL_ROUTES_YAML = (
    Path(__file__).resolve().parent.parent.parent / "factory" / "routes.yaml"
)

# ---------------------------------------------------------------------------
# Verified unified diff — produced by:
#   cp factory/routes.yaml /tmp/r/factory/routes.yaml
#   git init ... && git commit
#   sed -i 's/sm: deepseek.*/sm: deepseek\/deepseek-chat\n  max_tokens: 32000/' ...
#   git diff
# Applies cleanly to the exact factory/routes.yaml in this repo.  The test
# verifies this with git apply --check before proceeding.
# ---------------------------------------------------------------------------

_L3_PATCH = (
    "diff --git a/factory/routes.yaml b/factory/routes.yaml\n"
    "--- a/factory/routes.yaml\n"
    "+++ b/factory/routes.yaml\n"
    "@@ -24,6 +24,7 @@ routes:\n"
    "   analyst: deepseek/deepseek-chat\n"
    "   architect: anthropic/claude-opus-4-7\n"
    "   sm: deepseek/deepseek-chat\n"
    "+  max_tokens: 32000\n"
    "   dev:\n"
    "     standard: deepseek/deepseek-coder\n"
    "     hard: anthropic/claude-sonnet-4-6\n"
)

# ---------------------------------------------------------------------------
# Mocked LLM responses
# ---------------------------------------------------------------------------

_L1_RESPONSE = {
    "summary": (
        "Three SM persona calls failed in the last 15 minutes, all with "
        "error containing max_tokens=65536. Pattern: json parse failed at "
        "max_tokens=65536 across stories 110, 111, 112."
    ),
    "escalate_to_l2": True,
    "escalation_reason": (
        "Repeated SM token-overflow failures (3 distinct story IDs: 110, 111, 112). "
        "Pattern: max_tokens=65536."
    ),
    "observations": [
        {
            "detector": "runs_failed_since",
            "noteworthy": "3 SM failures with max_tokens=65536 error in window",
        },
        {"detector": "retry_storm", "noteworthy": "sm persona failure_count=3"},
        {"detector": "cost_spike", "noteworthy": None},
        {"detector": "tick_duration_outliers", "noteworthy": None},
        {"detector": "state_distribution_skew", "noteworthy": None},
        {"detector": "worktree_orphans", "noteworthy": None},
    ],
}

_L2_RESPONSE = {
    "title": "sm-max-tokens-overflow",
    "description": (
        "Three consecutive SM persona runs failed with max_tokens=65536 "
        "across stories 110, 111, 112. The SM persona is generating responses "
        "that hit the token ceiling. Each failure costs approximately $1.73."
    ),
    "evidence": [
        {
            "kind": "run",
            "id": RUN_STORY_IDS[0],
            "ts": T0.isoformat(),
            "excerpt": "sm failure max_tokens=65536",
        },
        {
            "kind": "run",
            "id": RUN_STORY_IDS[1],
            "ts": T1.isoformat(),
            "excerpt": "sm failure max_tokens=65536",
        },
        {
            "kind": "run",
            "id": RUN_STORY_IDS[2],
            "ts": T2.isoformat(),
            "excerpt": "sm failure max_tokens=65536",
        },
    ],
    "proposed_area": "persona_settings",
    "urgency": "warn",
    "escalate_to_l3": True,
    "escalation_reason": (
        "Repeated SM token-overflow failures across 3 distinct stories, no resolution."
    ),
}

_L3_RESPONSE = {
    "concern_title": "sm-max-tokens-overflow",
    "diagnosis": (
        "The SM persona is configured without an explicit max_tokens cap in "
        "factory/routes.yaml, so the model runs at the default ceiling "
        "(65536) and regularly hits finish_reason=length on dense stories. "
        "Adding max_tokens: 32000 under the routes block will halve the "
        "ceiling, preventing the overflow at the cost of slightly shorter "
        "outputs on edge-case stories."
    ),
    "proposal": {
        "kind": "persona_settings",
        "target": "factory/routes.yaml",
        "rationale": (
            "Lower SM max_tokens from the model default (65536) to 32000 "
            "to prevent token-overflow on dense stories."
        ),
        "suggested_patch": _L3_PATCH,
        "verification": "uv run pytest -q tests/test_handler_sm.py",
        "confidence": "medium",
    },
    "target_class": "persona_settings",
    "escalate_to_human": False,
    "escalation_reason": None,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _make_repo(tmp_path: Path) -> Path:
    """Create a git repo in tmp_path with factory/routes.yaml committed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    routes_dest = repo / "factory" / "routes.yaml"
    routes_dest.parent.mkdir(parents=True)
    routes_dest.write_text(_REAL_ROUTES_YAML.read_text(encoding="utf-8"), encoding="utf-8")
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


def _plant_run_failures(root: Path) -> None:
    """Write 3 SM persona failure events to state/events/runs.ndjson."""
    path = root / "state" / "events" / "runs.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamps = [T0, T1, T2]
    for i, ts in enumerate(timestamps):
        rec = {
            "ts": ts.isoformat(),
            "schema_version": 1,
            "event": "run",
            "success": False,
            "persona": "sm",
            "story_id": RUN_STORY_IDS[i],
            "cost_usd": 1.73,
            "error": f"json parse failed at max_tokens=65536 story_id={RUN_STORY_IDS[i]}",
            "model": "azure/gpt-5.4",
            "model_tier": None,
            "tokens_in": 8000,
            "tokens_out": 65536,
            "duration_s": 45.0,
            "attempt_n": 1,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")


def _make_l4_runner(
    *,
    test_rc: int = 0,
    push_rc: int = 0,
    pr_number: int = 42,
) -> tuple[Any, list[list[str]]]:
    """Create a test runner mocking external commands for the L4 apply step."""
    calls: list[list[str]] = []

    def _runner(args: list[str], **kwargs: Any) -> Any:
        calls.append(list(args))
        if args[:1] == ["uv"] and "pytest" in args:
            return _Completed(returncode=test_rc, stdout="ok")
        if args[:2] == ["git", "push"]:
            return _Completed(returncode=push_rc)
        if args[:3] == ["gh", "pr", "create"]:
            return _Completed(
                returncode=0,
                stdout=f"https://github.com/x/y/pull/{pr_number}\n",
            )
        if args[:3] == ["gh", "pr", "merge"]:
            return _Completed(returncode=0)
        if args[:3] == ["gh", "label", "create"]:
            return _Completed(returncode=0)
        # Real git for everything else.
        kwargs.pop("check", None)
        return subprocess.run(args, **kwargs)

    return _runner, calls


# ---------------------------------------------------------------------------
# Main chained integration test
# ---------------------------------------------------------------------------


def test_sm_overflow_full_chain_l1_l2_l3_l4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full chain: L1 → L2 → L3 → L4 with all LLM calls mocked.

    Verifies the SM-overflow synthetic incident flows from raw run failures
    through watcher escalation, concern production, proposal generation, and
    finally automatic PR creation — with exactly 3 text_run invocations.
    """
    # ------------------------------------------------------------------
    # Setup: temp repo + planted signals
    # ------------------------------------------------------------------
    repo = _make_repo(tmp_path)
    _plant_run_failures(repo)

    # Verify the L3 patch applies cleanly to this repo BEFORE running the
    # chain. If this fails, stop immediately rather than producing a
    # confusing downstream error.
    import tempfile as _tempfile

    with _tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, encoding="utf-8"
    ) as pf:
        patch_file = Path(pf.name)
        pf.write(_L3_PATCH)

    try:
        check = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", str(patch_file)],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0, (
            f"L3 patch does not apply cleanly to factory/routes.yaml in the temp repo.\n"
            f"stdout: {check.stdout}\nstderr: {check.stderr}\n"
            f"Patch:\n{_L3_PATCH}"
        )
    finally:
        patch_file.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Cost-bound counter — track text_run calls across all three modules
    # ------------------------------------------------------------------
    text_run_call_count = 0

    def _make_l1_mock():
        def _mock(persona, prompt, model_id, schema=None, **kwargs):
            nonlocal text_run_call_count
            text_run_call_count += 1
            return _L1_RESPONSE

        return _mock

    def _make_l2_mock():
        def _mock(persona, prompt, model_id, schema=None, **kwargs):
            nonlocal text_run_call_count
            text_run_call_count += 1
            return _L2_RESPONSE

        return _mock

    def _make_l3_mock():
        def _mock(persona, prompt, model_id, schema=None, **kwargs):
            nonlocal text_run_call_count
            text_run_call_count += 1
            return _L3_RESPONSE

        return _mock

    # Monkeypatch each module's own text_run wrapper independently so
    # the cost-bound check is per-module.
    monkeypatch.setattr("factory.manager.watcher.text_run", _make_l1_mock())
    monkeypatch.setattr("factory.manager.watcher._read_persona_prompt", lambda p: "# L1 mock")
    monkeypatch.setattr("factory.manager.summarizer.text_run", _make_l2_mock())
    monkeypatch.setattr("factory.manager.summarizer._read_persona_prompt", lambda p: "# L2 mock")
    monkeypatch.setattr("factory.manager.diagnostician.text_run", _make_l3_mock())
    monkeypatch.setattr("factory.manager.diagnostician._read_persona_prompt", lambda p: "# L3 mock")

    # ------------------------------------------------------------------
    # Step 1: L1 Watcher
    # ------------------------------------------------------------------
    l1_result = run_watcher_once(root=repo, now=NOW, lookback=timedelta(hours=2))

    l1_note = l1_result.get("note", {})
    assert l1_note.get("escalate_to_l2") is True, (
        f"L1 should have escalated to L2. note={l1_note}"
    )

    notes_path = repo / "state" / "events" / "watcher_notes.ndjson"
    assert notes_path.exists(), "watcher_notes.ndjson must be created by L1"
    lines = [ln for ln in notes_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, f"Expected exactly 1 watcher note, got {len(lines)}"

    # ------------------------------------------------------------------
    # Step 2: L2 Summarizer
    # ------------------------------------------------------------------
    l2_result = run_summarizer_once(root=repo, now=NOW + timedelta(seconds=1))
    assert l2_result is not None, "L2 should produce a concern (not None)"

    assert l2_result.get("urgency") == "warn", (
        f"L2 urgency should be 'warn', got {l2_result.get('urgency')!r}"
    )
    assert l2_result.get("escalate_to_l3") is True, (
        f"L2 should escalate to L3. result={l2_result}"
    )

    # Concern file must exist under state/concerns/.
    concerns_dir = repo / "state" / "concerns"
    concern_files = list(concerns_dir.glob("*.json")) if concerns_dir.exists() else []
    assert len(concern_files) == 1, (
        f"Expected exactly 1 concern file, got {len(concern_files)}"
    )

    # Evidence must reference the planted run IDs (or at least the timestamps).
    evidence = l2_result.get("evidence", [])
    assert len(evidence) >= 3, f"Expected at least 3 evidence items, got {len(evidence)}"
    evidence_ids = {e.get("id") for e in evidence if "id" in e}
    assert evidence_ids.issuperset({110, 111, 112}), (
        f"Evidence should reference run IDs 110, 111, 112. Got: {evidence_ids}"
    )

    # ------------------------------------------------------------------
    # Step 3: L3 Diagnostician
    # ------------------------------------------------------------------
    concern_path = Path(l2_result["concern_path"])
    l3_result = run_diagnostician_once(
        root=repo,
        concern_path=concern_path,
        now=NOW + timedelta(seconds=2),
    )
    assert l3_result is not None, "L3 should produce a proposal (not None)"

    assert l3_result.get("target_class") == "persona_settings", (
        f"L3 target_class should be 'persona_settings', got {l3_result.get('target_class')!r}"
    )
    suggested_patch = l3_result.get("proposal", {}).get("suggested_patch", "")
    assert suggested_patch.strip(), "L3 proposal should contain a non-empty patch"

    # Verify patch looks like a unified diff.
    import re

    assert re.search(r"^@@\s+-\d+", suggested_patch, re.MULTILINE), (
        f"L3 patch should contain a unified-diff hunk header. Got:\n{suggested_patch[:300]}"
    )

    # Proposal file must exist.
    proposals_dir = repo / "state" / "manager_proposals"
    proposal_files = list(proposals_dir.glob("*.json")) if proposals_dir.exists() else []
    assert len(proposal_files) == 1, (
        f"Expected exactly 1 proposal file, got {len(proposal_files)}"
    )

    # ------------------------------------------------------------------
    # Step 4: L4 Apply
    # ------------------------------------------------------------------
    mocked_runner, runner_calls = _make_l4_runner(pr_number=88)

    l4_result = apply_manager_proposals(
        root=repo,
        dry_run=False,
        runner=mocked_runner,
        repo="x/y",
        push=True,
    )

    # Classification must be safe.
    results = l4_result.get("results", [])
    assert results, "L4 must have processed at least one proposal"
    classification = results[0].get("classification")
    assert classification == "safe", (
        f"L4 classification should be 'safe', got {classification!r}"
    )

    # Branch must have been created.
    branch_calls = [c for c in runner_calls if c[:3] == ["git", "checkout", "-b"]]
    assert branch_calls, "L4 should create a branch (git checkout -b)"

    # Patch was applied — routes.yaml on the branch must contain max_tokens.
    history = _load_history(repo)
    assert history, "L4 must write a history entry"
    branch_name = history[0].get("branch")
    assert branch_name, "History entry must record branch name"

    show_proc = subprocess.run(
        ["git", "show", f"{branch_name}:factory/routes.yaml"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert show_proc.returncode == 0, (
        f"Could not show factory/routes.yaml on branch {branch_name}: {show_proc.stderr}"
    )
    assert "max_tokens" in show_proc.stdout, (
        f"Patch should have added max_tokens to routes.yaml on branch {branch_name}"
    )

    # Pytest was invoked.
    pytest_calls = [c for c in runner_calls if c[:1] == ["uv"] and "pytest" in c]
    assert pytest_calls, "L4 should invoke pytest"

    # gh pr create was called with safe label.
    from factory.chain.factory_improver_apply import SAFE_LABEL

    pr_create_calls = [c for c in runner_calls if c[:3] == ["gh", "pr", "create"]]
    assert pr_create_calls, "L4 should call gh pr create"
    pr_args = pr_create_calls[0]
    assert "--label" in pr_args, "gh pr create should include --label"
    label_idx = pr_args.index("--label")
    assert pr_args[label_idx + 1] == SAFE_LABEL, (
        f"Expected label {SAFE_LABEL!r}, got {pr_args[label_idx + 1]!r}"
    )

    # gh pr merge --auto --squash was called.
    pr_merge_calls = [c for c in runner_calls if c[:3] == ["gh", "pr", "merge"]]
    assert pr_merge_calls, "L4 should call gh pr merge for auto-merge"
    assert "--auto" in pr_merge_calls[0], "gh pr merge should include --auto"
    assert "--squash" in pr_merge_calls[0], "gh pr merge should include --squash"

    # History entry with status=opened_pr.
    assert any(
        h.get("status") == "opened_pr" and h.get("classification") == "safe"
        for h in history
    ), f"Expected opened_pr in history. Got: {history}"

    # ------------------------------------------------------------------
    # Cost-bound assertion: exactly 3 text_run calls total.
    # If > 3, something is silently retrying — fail loudly.
    # ------------------------------------------------------------------
    assert text_run_call_count == 3, (
        f"Expected exactly 3 text_run calls (L1×1 + L2×1 + L3×1). "
        f"Got {text_run_call_count}. Something is retrying unexpectedly."
    )
