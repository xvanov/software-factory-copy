"""Tests for failed-apply memory in factory.manager.diagnostician.

Test inventory
--------------
test_no_prior_failures_no_section_in_prompt
    Plant a concern, no prior history. Assert prompt does NOT contain
    "Prior failed attempts".

test_prior_failure_appears_in_prompt
    Plant a concern + a matching history entry with status=test_failed.
    Assert prompt contains "Prior failed attempts" AND the patch excerpt.

test_multiple_prior_failures_appear_in_prompt
    3 failed history entries, all three appear in prompt.

test_old_failures_outside_lookback_excluded
    Failure with ts older than 24h is NOT in the prompt.

test_unrelated_concern_failures_excluded
    Failure on a DIFFERENT concern title is not in the prompt.

test_successful_applies_not_included
    status=opened_pr history entry is excluded.

test_l3_sees_prior_failed_apply_in_chain
    Plant a failed-apply history entry, run a full L1→L2→L3 chain,
    assert L3's captured prompt contains the prior-failure section.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from factory.manager.diagnostician import (
    _load_recent_failed_applies,
    run_diagnostician_once,
)

# ---------------------------------------------------------------------------
# Constants shared with test_diagnostician.py pattern
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)

_CANNED_PROPOSAL_RESPONSE = {
    "concern_title": "sm-token-overflow-loop",
    "diagnosis": "Root cause: SM token overflow.",
    "proposal": {
        "kind": "persona_settings",
        "target": "factory/routes.yaml",
        "rationale": "Lower max_tokens.",
        "suggested_patch": (
            "diff --git a/factory/routes.yaml b/factory/routes.yaml\n"
            "--- a/factory/routes.yaml\n"
            "+++ b/factory/routes.yaml\n"
            "@@ -1,3 +1,4 @@\n"
            " routes:\n"
            "+  max_tokens: 32000\n"
            "   sm: deepseek/deepseek-chat\n"
        ),
        "verification": "uv run pytest tests/",
        "confidence": "medium",
    },
    "target_class": "persona_settings",
    "escalate_to_human": False,
    "escalation_reason": None,
}

_CANNED_CONCERN = {
    "schema_version": 1,
    "title": "sm-token-overflow-loop",
    "description": "Three SM persona calls failed with max_tokens=65536.",
    "evidence": [
        {"kind": "run", "id": 100, "ts": "2026-05-26T11:51:00+00:00", "excerpt": "sm failure"},
    ],
    "proposed_area": "persona_settings",
    "urgency": "warn",
    "escalate_to_l3": True,
    "escalation_reason": "Repeated SM token-overflow failures.",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_concern(root: Path, concern: dict[str, Any] | None = None) -> Path:
    concerns_dir = root / "state" / "concerns"
    concerns_dir.mkdir(parents=True, exist_ok=True)
    doc = concern if concern is not None else dict(_CANNED_CONCERN)
    path = concerns_dir / "20260526T115500-sm-token-overflow-loop.json"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _write_proposal_file(
    root: Path,
    concern_title: str,
    slug: str,
    suggested_patch: str = "",
    kind: str = "persona_settings",
    target: str = "factory/routes.yaml",
    ts_prefix: str = "20260526T110000",
) -> Path:
    """Write a proposal JSON under state/manager_proposals/."""
    proposals_dir = root / "state" / "manager_proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    proposal = {
        "schema_version": 1,
        "concern_title": concern_title,
        "diagnosis": "Diagnosed.",
        "proposal": {
            "kind": kind,
            "target": target,
            "rationale": "Fix it.",
            "suggested_patch": suggested_patch,
            "verification": "uv run pytest",
            "confidence": "medium",
        },
        "target_class": "persona_settings",
        "escalate_to_human": False,
        "escalation_reason": None,
    }
    path = proposals_dir / f"{ts_prefix}-{slug}.json"
    path.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    return path


def _write_history(root: Path, entries: list[dict[str, Any]]) -> None:
    """Write .manager_apply_history.json with the given entries."""
    history_path = root / "state" / ".manager_apply_history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _make_history_entry(
    proposal_path: str,
    status: str = "test_failed",
    ts: str | None = None,
) -> dict[str, Any]:
    if ts is None:
        ts = "2026-05-26T11:30:00+00:00"  # within 24h of NOW
    return {
        "proposal_path": proposal_path,
        "ts": ts,
        "branch": "factory-manager/20260526T113000-sm-token-overflow",
        "pr_url": None,
        "pr_number": None,
        "status": status,
        "classification": "safe",
    }


def _make_capturing_llm(response: dict[str, Any], captured_prompts: list[str]):
    def _mock(persona, prompt, model_id, schema=None, **kwargs):
        captured_prompts.append(prompt)
        return response
    return _mock


def _mock_persona_prompt(persona: str) -> str:
    return f"# {persona} mock persona"


def _mock_route(persona: str) -> str:
    return "anthropic/claude-opus-4-7"


def _mock_max_tokens(model_id: str) -> int:
    return 32768


def _patch_llm_infra(monkeypatch: pytest.MonkeyPatch, response: dict, captured: list[str]) -> None:
    monkeypatch.setattr(
        "factory.manager.diagnostician.text_run",
        _make_capturing_llm(response, captured),
    )
    monkeypatch.setattr(
        "factory.manager.diagnostician._read_persona_prompt",
        _mock_persona_prompt,
    )
    import factory.model_router as mr
    monkeypatch.setattr(mr, "route", _mock_route)
    monkeypatch.setattr(mr, "max_output_tokens_for", _mock_max_tokens)


# ---------------------------------------------------------------------------
# Unit tests: _load_recent_failed_applies
# ---------------------------------------------------------------------------


class TestLoadRecentFailedApplies:
    """Unit tests for _load_recent_failed_applies in isolation."""

    def test_no_history_file_returns_empty(self, tmp_path: Path) -> None:
        result = _load_recent_failed_applies(
            root=tmp_path,
            concern_title="sm-token-overflow-loop",
            now=NOW,
        )
        assert result == []

    def test_empty_history_returns_empty(self, tmp_path: Path) -> None:
        _write_history(tmp_path, [])
        result = _load_recent_failed_applies(
            root=tmp_path,
            concern_title="sm-token-overflow-loop",
            now=NOW,
        )
        assert result == []

    def test_matching_test_failed_entry_returned(self, tmp_path: Path) -> None:
        patch = "diff --git a/factory/routes.yaml b/factory/routes.yaml\n--- a/...\n+++ b/...\n@@ -1 +1 @@\n-old\n+new\n"
        proposal_path = _write_proposal_file(
            tmp_path,
            concern_title="sm-token-overflow-loop",
            slug="sm-token-overflow-loop",
            suggested_patch=patch,
        )
        _write_history(tmp_path, [
            _make_history_entry(str(proposal_path), status="test_failed"),
        ])

        result = _load_recent_failed_applies(
            root=tmp_path,
            concern_title="sm-token-overflow-loop",
            now=NOW,
        )
        assert len(result) == 1
        assert result[0]["status"] == "test_failed"
        assert result[0]["target"] == "factory/routes.yaml"
        assert result[0]["patch_excerpt"] == patch[:800]

    def test_abandoned_status_included(self, tmp_path: Path) -> None:
        proposal_path = _write_proposal_file(
            tmp_path,
            concern_title="sm-token-overflow-loop",
            slug="sm-token-overflow-loop",
        )
        _write_history(tmp_path, [
            _make_history_entry(str(proposal_path), status="abandoned"),
        ])
        result = _load_recent_failed_applies(
            root=tmp_path,
            concern_title="sm-token-overflow-loop",
            now=NOW,
        )
        assert len(result) == 1
        assert result[0]["status"] == "abandoned"

    def test_successful_status_excluded(self, tmp_path: Path) -> None:
        proposal_path = _write_proposal_file(
            tmp_path,
            concern_title="sm-token-overflow-loop",
            slug="sm-token-overflow-loop",
        )
        _write_history(tmp_path, [
            _make_history_entry(str(proposal_path), status="opened_pr"),
        ])
        result = _load_recent_failed_applies(
            root=tmp_path,
            concern_title="sm-token-overflow-loop",
            now=NOW,
        )
        assert result == []

    def test_old_entry_outside_lookback_excluded(self, tmp_path: Path) -> None:
        proposal_path = _write_proposal_file(
            tmp_path,
            concern_title="sm-token-overflow-loop",
            slug="sm-token-overflow-loop",
        )
        old_ts = (NOW - timedelta(hours=25)).isoformat()
        _write_history(tmp_path, [
            _make_history_entry(str(proposal_path), status="test_failed", ts=old_ts),
        ])
        result = _load_recent_failed_applies(
            root=tmp_path,
            concern_title="sm-token-overflow-loop",
            now=NOW,
        )
        assert result == []

    def test_unrelated_concern_excluded(self, tmp_path: Path) -> None:
        proposal_path = _write_proposal_file(
            tmp_path,
            concern_title="some-other-concern",
            slug="some-other-concern",
        )
        _write_history(tmp_path, [
            _make_history_entry(str(proposal_path), status="test_failed"),
        ])
        result = _load_recent_failed_applies(
            root=tmp_path,
            concern_title="sm-token-overflow-loop",
            now=NOW,
        )
        assert result == []

    def test_results_ordered_newest_first(self, tmp_path: Path) -> None:
        p1 = _write_proposal_file(
            tmp_path,
            concern_title="sm-token-overflow-loop",
            slug="sm-token-overflow-loop-v1",
            ts_prefix="20260526T100000",
        )
        p2 = _write_proposal_file(
            tmp_path,
            concern_title="sm-token-overflow-loop",
            slug="sm-token-overflow-loop-v2",
            ts_prefix="20260526T110000",
        )
        _write_history(tmp_path, [
            _make_history_entry(str(p1), ts="2026-05-26T10:00:00+00:00"),
            _make_history_entry(str(p2), ts="2026-05-26T11:00:00+00:00"),
        ])
        result = _load_recent_failed_applies(
            root=tmp_path,
            concern_title="sm-token-overflow-loop",
            now=NOW,
        )
        assert len(result) == 2
        assert result[0]["ts"] == "2026-05-26T11:00:00+00:00"
        assert result[1]["ts"] == "2026-05-26T10:00:00+00:00"

    def test_capped_at_five_entries(self, tmp_path: Path) -> None:
        entries = []
        for i in range(7):
            p = _write_proposal_file(
                tmp_path,
                concern_title="sm-token-overflow-loop",
                slug=f"sm-token-overflow-loop-v{i}",
                ts_prefix=f"20260526T1{i:02d}000",
            )
            entries.append(
                _make_history_entry(
                    str(p),
                    ts=f"2026-05-26T1{i:01d}:00:00+00:00",
                )
            )
        _write_history(tmp_path, entries)
        result = _load_recent_failed_applies(
            root=tmp_path,
            concern_title="sm-token-overflow-loop",
            now=NOW,
        )
        assert len(result) == 5

    def test_patch_excerpt_capped_at_800_chars(self, tmp_path: Path) -> None:
        long_patch = "+" + "x" * 2000
        proposal_path = _write_proposal_file(
            tmp_path,
            concern_title="sm-token-overflow-loop",
            slug="sm-token-overflow-loop",
            suggested_patch=long_patch,
        )
        _write_history(tmp_path, [
            _make_history_entry(str(proposal_path), status="test_failed"),
        ])
        result = _load_recent_failed_applies(
            root=tmp_path,
            concern_title="sm-token-overflow-loop",
            now=NOW,
        )
        assert len(result) == 1
        assert len(result[0]["patch_excerpt"]) == 800


# ---------------------------------------------------------------------------
# Integration tests: prompt content via capturing mock
# ---------------------------------------------------------------------------


class TestNoFailuresNoPriorSection:
    """No prior history → prompt does NOT contain 'Prior failed attempts'."""

    def test_no_prior_failures_no_section_in_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        concern_path = _write_concern(tmp_path)
        captured: list[str] = []
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL_RESPONSE, captured)

        run_diagnostician_once(root=tmp_path, concern_path=concern_path, now=NOW)

        assert len(captured) == 1
        assert "Prior failed attempts" not in captured[0]


class TestPriorFailureAppearsInPrompt:
    """Plant a matching history entry → prompt contains the section and excerpt."""

    def test_prior_failure_appears_in_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        concern_path = _write_concern(tmp_path)
        patch = (
            "diff --git a/factory/routes.yaml b/factory/routes.yaml\n"
            "--- a/factory/routes.yaml\n"
            "+++ b/factory/routes.yaml\n"
            "@@ -24,6 +24,7 @@ routes:\n"
            "   sm: deepseek/deepseek-chat\n"
            "+  max_tokens: 32000\n"
        )
        # Store the prior proposal in a separate subdir so it does NOT
        # trigger the "already processed" check in run_diagnostician_once.
        prior_dir = tmp_path / "state" / "prior_proposals"
        prior_dir.mkdir(parents=True, exist_ok=True)
        prior_proposal = {
            "schema_version": 1,
            "concern_title": "sm-token-overflow-loop",
            "diagnosis": "Prior attempt.",
            "proposal": {
                "kind": "persona_settings",
                "target": "factory/routes.yaml",
                "rationale": "Lower max_tokens.",
                "suggested_patch": patch,
                "verification": "uv run pytest",
                "confidence": "medium",
            },
            "target_class": "persona_settings",
            "escalate_to_human": False,
            "escalation_reason": None,
        }
        prior_path = prior_dir / "20260526T110000-sm-token-overflow-loop.json"
        prior_path.write_text(json.dumps(prior_proposal, indent=2), encoding="utf-8")

        _write_history(tmp_path, [
            _make_history_entry(str(prior_path), status="test_failed"),
        ])

        captured: list[str] = []
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL_RESPONSE, captured)

        run_diagnostician_once(root=tmp_path, concern_path=concern_path, now=NOW)

        assert len(captured) == 1
        prompt = captured[0]
        assert "Prior failed attempts" in prompt, (
            "Prompt must contain 'Prior failed attempts' section"
        )
        assert "test_failed" in prompt
        # Patch excerpt should appear.
        assert "max_tokens: 32000" in prompt, (
            "Patch excerpt content should appear in the prompt"
        )

    def test_section_contains_target_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        concern_path = _write_concern(tmp_path)
        prior_dir = tmp_path / "state" / "prior_proposals"
        prior_dir.mkdir(parents=True, exist_ok=True)
        prior_proposal = {
            "schema_version": 1,
            "concern_title": "sm-token-overflow-loop",
            "diagnosis": "Diagnosed.",
            "proposal": {
                "kind": "persona_settings",
                "target": "factory/routes.yaml",
                "rationale": "Fix.",
                "suggested_patch": "",
                "verification": "uv run pytest",
                "confidence": "medium",
            },
            "target_class": "persona_settings",
            "escalate_to_human": False,
            "escalation_reason": None,
        }
        prior_path = prior_dir / "20260526T110000-sm-token-overflow-loop.json"
        prior_path.write_text(json.dumps(prior_proposal, indent=2), encoding="utf-8")

        _write_history(tmp_path, [
            _make_history_entry(str(prior_path), status="abandoned"),
        ])

        captured: list[str] = []
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL_RESPONSE, captured)
        run_diagnostician_once(root=tmp_path, concern_path=concern_path, now=NOW)

        prompt = captured[0]
        assert "factory/routes.yaml" in prompt


class TestMultiplePriorFailuresAppearInPrompt:
    """3 failed history entries, all appear in prompt."""

    def test_multiple_prior_failures_appear_in_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        concern_path = _write_concern(tmp_path)

        # Write prior proposals in a separate dir to avoid the "already processed" check.
        prior_dir = tmp_path / "state" / "prior_proposals"
        prior_dir.mkdir(parents=True, exist_ok=True)

        entries = []
        for i in range(3):
            patch = f"diff --git a/factory/routes.yaml b/factory/routes.yaml\n+attempt-{i}\n"
            prior_proposal = {
                "schema_version": 1,
                "concern_title": "sm-token-overflow-loop",
                "diagnosis": "Diagnosed.",
                "proposal": {
                    "kind": "persona_settings",
                    "target": "factory/routes.yaml",
                    "rationale": "Fix.",
                    "suggested_patch": patch,
                    "verification": "uv run pytest",
                    "confidence": "medium",
                },
                "target_class": "persona_settings",
                "escalate_to_human": False,
                "escalation_reason": None,
            }
            prior_path = prior_dir / f"20260526T10{i:02d}00-sm-v{i}.json"
            prior_path.write_text(json.dumps(prior_proposal, indent=2), encoding="utf-8")
            entries.append(
                _make_history_entry(
                    str(prior_path),
                    status="test_failed",
                    ts=f"2026-05-26T10:0{i}:00+00:00",
                )
            )
        _write_history(tmp_path, entries)

        captured: list[str] = []
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL_RESPONSE, captured)
        run_diagnostician_once(root=tmp_path, concern_path=concern_path, now=NOW)

        prompt = captured[0]
        assert "Prior failed attempts" in prompt
        # All 3 attempts should be in the prompt.
        assert "attempt-0" in prompt
        assert "attempt-1" in prompt
        assert "attempt-2" in prompt
        # The section should mention 3 proposals.
        assert "3 proposal(s)" in prompt


class TestOldFailuresExcluded:
    """Failure with ts > 24h ago is excluded."""

    def test_old_failures_outside_lookback_excluded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        concern_path = _write_concern(tmp_path)

        prior_dir = tmp_path / "state" / "prior_proposals"
        prior_dir.mkdir(parents=True, exist_ok=True)
        prior_proposal = {
            "schema_version": 1,
            "concern_title": "sm-token-overflow-loop",
            "diagnosis": "Old attempt.",
            "proposal": {
                "kind": "persona_settings",
                "target": "factory/routes.yaml",
                "rationale": "Fix.",
                "suggested_patch": "diff --git a/factory/routes.yaml b/factory/routes.yaml\n+old\n",
                "verification": "uv run pytest",
                "confidence": "medium",
            },
            "target_class": "persona_settings",
            "escalate_to_human": False,
            "escalation_reason": None,
        }
        prior_path = prior_dir / "20260525T110000-sm.json"
        prior_path.write_text(json.dumps(prior_proposal, indent=2), encoding="utf-8")

        old_ts = (NOW - timedelta(hours=25)).isoformat()
        _write_history(tmp_path, [
            _make_history_entry(str(prior_path), status="test_failed", ts=old_ts),
        ])

        captured: list[str] = []
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL_RESPONSE, captured)
        run_diagnostician_once(root=tmp_path, concern_path=concern_path, now=NOW)

        prompt = captured[0]
        assert "Prior failed attempts" not in prompt


class TestUnrelatedConcernExcluded:
    """Failure on a different concern title is not shown."""

    def test_unrelated_concern_failures_excluded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        concern_path = _write_concern(tmp_path)

        prior_dir = tmp_path / "state" / "prior_proposals"
        prior_dir.mkdir(parents=True, exist_ok=True)
        prior_proposal = {
            "schema_version": 1,
            "concern_title": "completely-different-concern",
            "diagnosis": "Different.",
            "proposal": {
                "kind": "persona_settings",
                "target": "factory/routes.yaml",
                "rationale": "Fix.",
                "suggested_patch": "diff --git a/x b/x\n+line\n",
                "verification": "uv run pytest",
                "confidence": "medium",
            },
            "target_class": "persona_settings",
            "escalate_to_human": False,
            "escalation_reason": None,
        }
        prior_path = prior_dir / "20260526T110000-other.json"
        prior_path.write_text(json.dumps(prior_proposal, indent=2), encoding="utf-8")

        _write_history(tmp_path, [
            _make_history_entry(str(prior_path), status="test_failed"),
        ])

        captured: list[str] = []
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL_RESPONSE, captured)
        run_diagnostician_once(root=tmp_path, concern_path=concern_path, now=NOW)

        prompt = captured[0]
        assert "Prior failed attempts" not in prompt


class TestSuccessfulAppliesNotIncluded:
    """status=opened_pr entry is excluded."""

    def test_successful_applies_not_included(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        concern_path = _write_concern(tmp_path)

        prior_dir = tmp_path / "state" / "prior_proposals"
        prior_dir.mkdir(parents=True, exist_ok=True)
        prior_proposal = {
            "schema_version": 1,
            "concern_title": "sm-token-overflow-loop",
            "diagnosis": "Successful apply.",
            "proposal": {
                "kind": "persona_settings",
                "target": "factory/routes.yaml",
                "rationale": "Fix.",
                "suggested_patch": "diff --git a/factory/routes.yaml b/factory/routes.yaml\n+line\n",
                "verification": "uv run pytest",
                "confidence": "medium",
            },
            "target_class": "persona_settings",
            "escalate_to_human": False,
            "escalation_reason": None,
        }
        prior_path = prior_dir / "20260526T110000-sm.json"
        prior_path.write_text(json.dumps(prior_proposal, indent=2), encoding="utf-8")

        _write_history(tmp_path, [
            _make_history_entry(str(prior_path), status="opened_pr"),
        ])

        captured: list[str] = []
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL_RESPONSE, captured)
        run_diagnostician_once(root=tmp_path, concern_path=concern_path, now=NOW)

        prompt = captured[0]
        assert "Prior failed attempts" not in prompt


# ---------------------------------------------------------------------------
# Integration chain test: L1 → L2 → L3 with prior failure planted
# ---------------------------------------------------------------------------


# Minimal LLM responses for chain test (same as test_integration_chain.py pattern).
_L1_RESPONSE = {
    "summary": "Three SM failures with max_tokens=65536.",
    "escalate_to_l2": True,
    "escalation_reason": "Repeated SM token-overflow.",
    "observations": [
        {"detector": "runs_failed_since", "noteworthy": "3 SM failures"},
        {"detector": "retry_storm", "noteworthy": "sm failure_count=3"},
        {"detector": "cost_spike", "noteworthy": None},
        {"detector": "tick_duration_outliers", "noteworthy": None},
        {"detector": "state_distribution_skew", "noteworthy": None},
        {"detector": "worktree_orphans", "noteworthy": None},
    ],
}

_L2_RESPONSE = {
    "title": "sm-token-overflow-loop",
    "description": "Three SM persona runs failed.",
    "evidence": [
        {"kind": "run", "id": 100, "ts": (NOW - timedelta(minutes=30)).isoformat(), "excerpt": "sm failure"},
    ],
    "proposed_area": "persona_settings",
    "urgency": "warn",
    "escalate_to_l3": True,
    "escalation_reason": "Repeated failures.",
}

_L3_PROPOSAL = {
    "concern_title": "sm-token-overflow-loop",
    "diagnosis": "SM token overflow.",
    "proposal": {
        "kind": "persona_settings",
        "target": "factory/routes.yaml",
        "rationale": "Lower max_tokens.",
        "suggested_patch": (
            "diff --git a/factory/routes.yaml b/factory/routes.yaml\n"
            "--- a/factory/routes.yaml\n"
            "+++ b/factory/routes.yaml\n"
            "@@ -24,6 +24,7 @@ routes:\n"
            "   sm: deepseek/deepseek-chat\n"
            "+  max_tokens: 32000\n"
        ),
        "verification": "uv run pytest tests/",
        "confidence": "medium",
    },
    "target_class": "persona_settings",
    "escalate_to_human": False,
    "escalation_reason": None,
}


def _make_chain_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with factory/routes.yaml committed."""
    real_routes = (
        Path(__file__).resolve().parent.parent.parent / "factory" / "routes.yaml"
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    routes_dest = repo / "factory" / "routes.yaml"
    routes_dest.parent.mkdir(parents=True)
    routes_dest.write_text(real_routes.read_text(encoding="utf-8"), encoding="utf-8")
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


def _plant_run_failures_chain(root: Path, now: datetime) -> None:
    path = root / "state" / "events" / "runs.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        rec = {
            "ts": (now - timedelta(minutes=50 - i * 16)).isoformat(),
            "schema_version": 1,
            "event": "run",
            "success": False,
            "persona": "sm",
            "story_id": 200 + i,
            "cost_usd": 1.73,
            "error": f"json parse failed at max_tokens=65536 story_id={200 + i}",
            "model": "azure/gpt-5.4",
            "model_tier": None,
            "tokens_in": 8000,
            "tokens_out": 65536,
            "duration_s": 45.0,
            "attempt_n": 1,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")


def test_l3_sees_prior_failed_apply_in_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1 → L2 → L3 chain: L3 prompt contains prior-failure section.

    Plant a failed-apply history entry referencing a proposal for
    'sm-token-overflow-loop' (the title L2 will emit).  Then run the
    full L1→L2→L3 chain with capturing mocks and assert that the L3
    prompt contains the "Prior failed attempts" section.
    """
    from factory.manager.diagnostician import run_diagnostician_once
    from factory.manager.summarizer import run_summarizer_once
    from factory.manager.watcher import run_watcher_once

    repo = _make_chain_repo(tmp_path)
    _plant_run_failures_chain(repo, NOW)

    # Plant an existing failed proposal for the same concern title that L2 will emit.
    prior_patch = (
        "diff --git a/factory/routes.yaml b/factory/routes.yaml\n"
        "--- a/factory/routes.yaml\n"
        "+++ b/factory/routes.yaml\n"
        "@@ -24,6 +24,7 @@ routes:\n"
        "   sm: deepseek/deepseek-chat\n"
        "+  max_tokens: 50000\n"
    )
    prior_proposal_path = _write_proposal_file(
        repo,
        concern_title="sm-token-overflow-loop",
        slug="sm-token-overflow-loop",
        suggested_patch=prior_patch,
        ts_prefix="20260526T110000",
    )
    _write_history(repo, [
        _make_history_entry(
            str(prior_proposal_path),
            status="test_failed",
            ts="2026-05-26T11:00:00+00:00",
        ),
    ])

    # Wire up capturing mock for L3.
    captured_l3_prompts: list[str] = []

    monkeypatch.setattr(
        "factory.manager.watcher.text_run",
        lambda p, pr, m, schema=None, **kw: _L1_RESPONSE,
    )
    monkeypatch.setattr("factory.manager.watcher._read_persona_prompt", lambda p: "# L1")
    monkeypatch.setattr(
        "factory.manager.summarizer.text_run",
        lambda p, pr, m, schema=None, **kw: _L2_RESPONSE,
    )
    monkeypatch.setattr("factory.manager.summarizer._read_persona_prompt", lambda p: "# L2")
    monkeypatch.setattr(
        "factory.manager.diagnostician.text_run",
        _make_capturing_llm(_L3_PROPOSAL, captured_l3_prompts),
    )
    monkeypatch.setattr("factory.manager.diagnostician._read_persona_prompt", lambda p: "# L3")
    import factory.model_router as mr
    monkeypatch.setattr(mr, "route", lambda *a, **kw: "anthropic/claude-opus-4-7")
    monkeypatch.setattr(mr, "max_output_tokens_for", lambda *a, **kw: 32768)

    # Run L1.
    l1_result = run_watcher_once(
        root=repo, now=NOW, lookback=timedelta(hours=2)
    )
    assert l1_result.get("note", {}).get("escalate_to_l2") is True

    # Run L2.
    l2_result = run_summarizer_once(root=repo, now=NOW + timedelta(seconds=1))
    assert l2_result is not None
    assert l2_result.get("escalate_to_l3") is True
    assert l2_result.get("title") == "sm-token-overflow-loop"

    # Run L3 — the planted prior failure should appear in its prompt.
    concern_path = Path(l2_result["concern_path"])
    l3_result = run_diagnostician_once(
        root=repo,
        concern_path=concern_path,
        now=NOW + timedelta(seconds=2),
    )
    assert l3_result is not None, "L3 must produce a proposal"

    assert captured_l3_prompts, "L3 mock must have captured a prompt"
    l3_prompt = captured_l3_prompts[0]

    assert "Prior failed attempts" in l3_prompt, (
        "L3 prompt must contain the 'Prior failed attempts' section when a "
        "failed apply entry exists for this concern title.\n"
        f"Prompt snippet: {l3_prompt[:500]}"
    )
    assert "test_failed" in l3_prompt
    assert "max_tokens: 50000" in l3_prompt, (
        "L3 prompt should include the patch excerpt from the prior failed attempt"
    )
