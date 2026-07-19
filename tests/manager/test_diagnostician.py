"""Tests for factory.manager.diagnostician — L3 Diagnostician agent (Phase 5).

All LLM calls are mocked; tests are deterministic.

Test inventory
--------------
test_no_concerns_returns_none
    Empty state/concerns/ → None.

test_run_diagnostician_once_writes_proposal
    Plant a concern, mock LLM (capturing), confirm proposal file +
    proposals.ndjson line.

test_diagnostician_prompt_contains_concern_and_source
    Capturing mock; assert prompt contains the concern's title + diagnosis
    + AT LEAST ONE pre-loaded source file content snippet.

test_pre_load_source_respects_proposed_area
    Five variants, one per proposed_area. Check the file set returned by
    _pre_load_source.

test_invalid_json_response_returns_sentinel_escalation
    LLM returns garbage → sentinel proposal with target_class="escalate_to_human".

test_diagnostician_skips_already_processed_concerns
    Write a concern + a matching proposal already in state/manager_proposals/.
    Run run_diagnostician_once → None.

test_dry_run_does_not_call_llm
    Dry-run mode does not call LLM.

test_watch_daemon_triggers_l3_on_l2_escalation
    Extends Phase 4's similar test for L3.

test_no_l3_flag_suppresses_l3
    --no-l3 suppresses L3 trigger.

test_sm_overflow_concern_produces_persona_settings_proposal (MVP)
    Plant SM-overflow concern, mock L3 to return a persona_settings proposal,
    assert all acceptance criteria from the spec.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from factory.manager.diagnostician import (
    _pre_load_source,
    _proposals_dir,
    _proposals_event_path,
    run_diagnostician_once,
)
from factory.manager.watcher import run_watcher_daemon

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)

# Canonical canned proposal from the L3 LLM.
_CANNED_PROPOSAL = {
    "concern_title": "sm-token-overflow-loop",
    "diagnosis": (
        "The SM persona (azure/gpt-5.4) is generating responses that exceed "
        "the max_tokens=65536 limit. The root cause is that the persona prompt "
        "instructs the model to produce exhaustive acceptance criteria lists "
        "without a token budget guard. The evidence in the concern shows three "
        "consecutive failures with finish_reason=length and cost_usd≈1.73 each. "
        "The mechanism: the persona prompt requests full YAML output for large "
        "story scopes, causing the model to hit the ceiling on dense stories. "
        "A smaller max_tokens cap or a prompt guard clause would prevent the loop."
    ),
    "proposal": {
        "kind": "persona_settings",
        "target": "factory/personas/sm.md",
        "rationale": (
            "Adding a token budget guard to the SM persona prompt will prevent "
            "the model from attempting to output more than the context ceiling. "
            "This directly addresses the root cause: unconstrained output length "
            "on dense stories."
        ),
        "suggested_patch": (
            "diff --git a/factory/personas/sm.md b/factory/personas/sm.md\n"
            "--- a/factory/personas/sm.md\n"
            "+++ b/factory/personas/sm.md\n"
            "@@ -1,4 +1,6 @@\n"
            " # Story Manager persona — `sm`\n"
            " \n"
            "+<!-- TOKEN BUDGET GUARD: Keep total output under 60KB. If stories\n"
            "+     are too dense, split them across multiple SM invocations. -->\n"
            " You are the Story Manager...\n"
        ),
        "verification": "uv run pytest tests/",
        "confidence": "high",
    },
    "target_class": "persona_settings",
    "escalate_to_human": False,
    "escalation_reason": None,
}

# A canned concern (what L2 would write to state/concerns/).
_CANNED_CONCERN = {
    "schema_version": 1,
    "title": "sm-token-overflow-loop",
    "description": (
        "Three consecutive SM persona calls failed with json parse failed at max_tokens=65536. "
        "The pattern began approximately 10 minutes ago and has affected stories 100, 101, and 102. "
        "Each failure results in a story rollback to story_created and a retry, burning approximately "
        "$1.73 per attempt."
    ),
    "evidence": [
        {"kind": "run", "id": 100, "ts": "2026-05-26T11:51:00+00:00", "excerpt": "sm failure max_tokens=65536"},
        {"kind": "run", "id": 101, "ts": "2026-05-26T11:53:00+00:00", "excerpt": "sm failure max_tokens=65536"},
        {"kind": "run", "id": 102, "ts": "2026-05-26T11:55:00+00:00", "excerpt": "sm failure max_tokens=65536"},
    ],
    "proposed_area": "persona_settings",
    "urgency": "warn",
    "escalate_to_l3": True,
    "escalation_reason": "Repeated SM token-overflow failures across 3 distinct stories, no resolution.",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_concern(root: Path, concern: dict[str, Any] | None = None, slug: str = "sm-token-overflow-loop") -> Path:
    """Write a concern JSON to state/concerns/."""
    concerns_dir = root / "state" / "concerns"
    concerns_dir.mkdir(parents=True, exist_ok=True)
    doc = concern if concern is not None else dict(_CANNED_CONCERN)
    path = concerns_dir / f"20260526T115500-{slug}.json"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _write_existing_proposal(root: Path, concern_title: str) -> Path:
    """Write a proposal to state/manager_proposals/ matching a concern title."""
    proposals_dir = _proposals_dir(root)
    proposals_dir.mkdir(parents=True, exist_ok=True)
    proposal = {
        "schema_version": 1,
        "concern_title": concern_title,
        "diagnosis": "Already diagnosed.",
        "proposal": {
            "kind": "prompt_edit",
            "target": "factory/personas/sm.md",
            "rationale": "Already done.",
            "suggested_patch": "diff ...",
            "verification": "uv run pytest",
            "confidence": "high",
        },
        "target_class": "prompt_edit",
        "escalate_to_human": False,
        "escalation_reason": None,
    }
    path = proposals_dir / f"20260526T120000-{concern_title}.json"
    path.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    return path


def _make_mock_llm(response: dict[str, Any]):
    """Return a callable that returns ``response`` — simulating schema-mode text_run."""

    def _mock_text_run(
        persona: str,
        prompt: str,
        model_id: str,
        schema: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return response

    return _mock_text_run


def _make_capturing_llm(response: dict[str, Any], captured_prompts: list[str]):
    """Return a callable that captures the prompt and returns ``response``."""

    def _mock_text_run(
        persona: str,
        prompt: str,
        model_id: str,
        schema: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured_prompts.append(prompt)
        return response

    return _mock_text_run


def _mock_persona_prompt(persona: str) -> str:
    return f"# {persona} mock persona"


def _mock_route(persona: str) -> str:
    return "anthropic/claude-opus-4-7"


def _mock_max_tokens(model_id: str) -> int:
    return 32768


def _patch_llm_infra(monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]) -> None:
    """Patch all LLM-related infrastructure for diagnostician tests."""
    monkeypatch.setattr(
        "factory.manager.diagnostician.text_run",
        _make_mock_llm(response),
    )
    monkeypatch.setattr(
        "factory.manager.diagnostician._read_persona_prompt",
        _mock_persona_prompt,
    )
    # Patch model_router functions called inside run_diagnostician_once
    import factory.model_router as mr
    monkeypatch.setattr(mr, "route", _mock_route)
    monkeypatch.setattr(mr, "max_output_tokens_for", _mock_max_tokens)


# ---------------------------------------------------------------------------
# Tests: basic returns-none cases
# ---------------------------------------------------------------------------


class TestNoConcernsReturnsNone:
    """Empty state/concerns/ → None."""

    def test_no_concerns_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL)
        result = run_diagnostician_once(root=tmp_path, now=NOW)
        assert result is None

    def test_empty_concerns_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        concerns_dir = tmp_path / "state" / "concerns"
        concerns_dir.mkdir(parents=True)
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL)
        result = run_diagnostician_once(root=tmp_path, now=NOW)
        assert result is None

    def test_concern_with_wrong_schema_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        concern = dict(_CANNED_CONCERN, schema_version=99)
        _write_concern(tmp_path, concern)
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL)
        result = run_diagnostician_once(root=tmp_path, now=NOW)
        assert result is None


# ---------------------------------------------------------------------------
# Tests: writes proposal
# ---------------------------------------------------------------------------


class TestRunDiagnosticianOnceWritesProposal:
    """Plant concern, mock LLM, confirm proposal file + proposals.ndjson."""

    def test_proposal_file_written(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_concern(tmp_path)
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL)

        result = run_diagnostician_once(root=tmp_path, now=NOW)

        assert result is not None
        assert "proposal_path" in result
        proposal_path = Path(result["proposal_path"])
        assert proposal_path.exists()

        written = json.loads(proposal_path.read_text())
        assert written["concern_title"] == _CANNED_PROPOSAL["concern_title"]
        assert written["target_class"] == _CANNED_PROPOSAL["target_class"]

    def test_proposals_ndjson_appended(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_concern(tmp_path)
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL)

        run_diagnostician_once(root=tmp_path, now=NOW)

        event_path = _proposals_event_path(tmp_path)
        assert event_path.exists()
        lines = [ln for ln in event_path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1

        event = json.loads(lines[0])
        assert event["event"] == "proposal_emitted"
        assert event["schema_version"] == 1
        assert event["concern_title"] == _CANNED_PROPOSAL["concern_title"]
        assert "proposal_path" in event

    def test_return_value_has_expected_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_concern(tmp_path)
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL)

        result = run_diagnostician_once(root=tmp_path, now=NOW)

        assert result is not None
        assert result["concern_title"] == "sm-token-overflow-loop"
        assert result["target_class"] == "persona_settings"
        assert "proposal" in result
        assert result["proposal"]["kind"] == "persona_settings"


# ---------------------------------------------------------------------------
# Tests: prompt content (load-bearing)
# ---------------------------------------------------------------------------


class TestDiagnosticianPromptContainsConcernAndSource:
    """Capturing mock; assert prompt contains concern title + source file content."""

    def test_prompt_contains_concern_title(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_concern(tmp_path)
        captured: list[str] = []
        monkeypatch.setattr(
            "factory.manager.diagnostician.text_run",
            _make_capturing_llm(_CANNED_PROPOSAL, captured),
        )
        monkeypatch.setattr(
            "factory.manager.diagnostician._read_persona_prompt",
            _mock_persona_prompt,
        )
        import factory.model_router as mr
        monkeypatch.setattr(mr, "route", _mock_route)
        monkeypatch.setattr(mr, "max_output_tokens_for", _mock_max_tokens)

        run_diagnostician_once(root=tmp_path, now=NOW)

        assert len(captured) == 1
        prompt = captured[0]
        assert "sm-token-overflow-loop" in prompt

    def test_prompt_contains_concern_description(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_concern(tmp_path)
        captured: list[str] = []
        monkeypatch.setattr(
            "factory.manager.diagnostician.text_run",
            _make_capturing_llm(_CANNED_PROPOSAL, captured),
        )
        monkeypatch.setattr(
            "factory.manager.diagnostician._read_persona_prompt",
            _mock_persona_prompt,
        )
        import factory.model_router as mr
        monkeypatch.setattr(mr, "route", _mock_route)
        monkeypatch.setattr(mr, "max_output_tokens_for", _mock_max_tokens)

        run_diagnostician_once(root=tmp_path, now=NOW)

        prompt = captured[0]
        # The concern description should appear in the prompt.
        assert "max_tokens=65536" in prompt

    def test_prompt_contains_at_least_one_source_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """For proposed_area=persona_settings, routes.yaml should be in the prompt."""
        _write_concern(tmp_path)
        captured: list[str] = []
        monkeypatch.setattr(
            "factory.manager.diagnostician.text_run",
            _make_capturing_llm(_CANNED_PROPOSAL, captured),
        )
        monkeypatch.setattr(
            "factory.manager.diagnostician._read_persona_prompt",
            _mock_persona_prompt,
        )
        import factory.model_router as mr
        monkeypatch.setattr(mr, "route", _mock_route)
        monkeypatch.setattr(mr, "max_output_tokens_for", _mock_max_tokens)

        run_diagnostician_once(root=tmp_path, now=NOW)

        prompt = captured[0]
        # For persona_settings, routes.yaml is included.
        # The prompt should mention at least one factory source file.
        assert "routes.yaml" in prompt or "factory/personas" in prompt or ".md" in prompt


# ---------------------------------------------------------------------------
# Tests: _pre_load_source per proposed_area
# ---------------------------------------------------------------------------


class TestPreLoadSourceRespectsProposedArea:
    """Five variants of _pre_load_source, one per proposed_area."""

    @property
    def _factory_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent / "factory"

    def test_prompt_area_loads_personas(self) -> None:
        files = _pre_load_source("prompt", factory_dir=self._factory_dir)
        # Should include persona .md files.
        assert any("personas/" in k or "personas\\" in k for k in files)
        assert any(k.endswith(".md") for k in files)

    def test_prompt_edit_area_loads_personas(self) -> None:
        files = _pre_load_source("prompt_edit", factory_dir=self._factory_dir)
        assert any(k.endswith(".md") for k in files)

    def test_persona_settings_area_loads_routes_and_personas(self) -> None:
        files = _pre_load_source("persona_settings", factory_dir=self._factory_dir)
        assert any("routes.yaml" in k for k in files)
        assert any(k.endswith(".md") for k in files)

    def test_dispatch_code_area_loads_chain_files(self) -> None:
        files = _pre_load_source("dispatch_code", factory_dir=self._factory_dir)
        assert any("orchestrator.py" in k for k in files)
        assert any("handlers.py" in k for k in files)
        assert any("state_machine.py" in k for k in files)

    def test_detector_tool_area_loads_detectors(self) -> None:
        files = _pre_load_source("detector_tool", factory_dir=self._factory_dir)
        # Should include detector .py files.
        assert any("detectors/" in k or "detectors\\" in k for k in files)
        assert any("signals.py" in k for k in files)

    def test_observability_area_loads_observability_and_signals(self) -> None:
        files = _pre_load_source("observability", factory_dir=self._factory_dir)
        # Should include signals.py and observability files.
        assert any("signals.py" in k for k in files)

    def test_unknown_area_loads_key_files(self) -> None:
        files = _pre_load_source("unknown", factory_dir=self._factory_dir)
        # Should include the file listing.
        assert any("factory-file-listing" in k or "orchestrator.py" in k for k in files)

    def test_file_cap_applied(self) -> None:
        """Each dispatch_code file is capped at the 160KB per-file cap."""
        files = _pre_load_source("dispatch_code", factory_dir=self._factory_dir)
        for content in files.values():
            assert len(content) <= 160 * 1024 + 100  # small buffer for the truncation notice

    def test_dispatch_code_loads_handlers_whole_against_real_repo(self) -> None:
        """Regression guard: the whole point of widening the dispatch_code
        bundle is that L3 sees the real files. A pre-existing 100KB total-bundle
        enforcement used to re-shrink every file to ~5KB (worse than the old
        3-file bundle). Against the REAL factory/ tree, handlers.py (~137KB) and
        the small git files must load essentially whole — this test uses real
        file sizes precisely because synthetic tiny fixtures never trip the
        enforcement that caused the regression."""
        files = _pre_load_source("dispatch_code", factory_dir=self._factory_dir)
        handlers = next((v for k, v in files.items() if k.endswith("chain/handlers.py")), "")
        # handlers.py is ~137KB on disk; must NOT be shredded down toward the
        # old 100KB/len(files) ~= 5KB share.
        assert len(handlers) > 100 * 1024, f"handlers.py shredded to {len(handlers)} chars"
        for name in ("worktree.py", "branch.py", "rollback.py", "auto_merge.py"):
            assert any(
                k.endswith(f"chain/{name}") for k in files
            ), f"{name} missing from dispatch_code bundle"

    def test_narrow_area_still_trimmed(self) -> None:
        """persona-loading areas keep the 100KB ceiling (prompt_edit loads all
        personas and is meant to be trimmed) — the wide ceiling is chain-only."""
        files = _pre_load_source("prompt_edit", factory_dir=self._factory_dir)
        total = sum(len(v) for v in files.values())
        assert total <= 100 * 1024 + 5000  # _BUNDLE_TOTAL_CAP + truncation notices

    def test_dispatch_code_area_widens_bundle_to_chain_dir(self, tmp_path: Path) -> None:
        """The dispatch_code bundle must no longer be a 3-file allowlist:
        auto_merge.py and worktree.py (previously omitted entirely) must be
        loadable. Uses a synthetic factory/chain/ dir sized so every file
        fits comfortably within the bundle budget, independent of how large
        the real repo's chain/ files happen to be."""
        factory_dir = tmp_path / "factory"
        chain_dir = factory_dir / "chain"
        chain_dir.mkdir(parents=True)
        for name in (
            "orchestrator.py",
            "handlers.py",
            "state_machine.py",
            "auto_merge.py",
            "worktree.py",
            "branch.py",
            "rollback.py",
            "recovery.py",
        ):
            (chain_dir / name).write_text(f"# {name}\ncontent\n", encoding="utf-8")

        files = _pre_load_source("dispatch_code", factory_dir=factory_dir, root=tmp_path)

        assert any("chain/auto_merge.py" in k for k in files)
        assert any("chain/worktree.py" in k for k in files)
        assert any("chain/branch.py" in k for k in files)
        assert any("chain/rollback.py" in k for k in files)
        assert any("chain/recovery.py" in k for k in files)

    def test_dispatch_code_priority_files_load_before_budget_cutoff(self, tmp_path: Path) -> None:
        """orchestrator.py, handlers.py, state_machine.py, auto_merge.py must
        always be included even when the rest of chain/ blows the bundle
        budget."""
        from factory.manager.diagnostician import _DISPATCH_CODE_BUNDLE_CAP

        factory_dir = tmp_path / "factory"
        chain_dir = factory_dir / "chain"
        chain_dir.mkdir(parents=True)
        big = "x" * (_DISPATCH_CODE_BUNDLE_CAP // 2)
        for name in ("orchestrator.py", "handlers.py", "state_machine.py", "auto_merge.py"):
            (chain_dir / name).write_text(big, encoding="utf-8")
        # Padding files that would blow the remaining budget on their own.
        for i in range(5):
            (chain_dir / f"zzz_padding_{i}.py").write_text(big, encoding="utf-8")

        files = _pre_load_source("dispatch_code", factory_dir=factory_dir, root=tmp_path)

        for name in ("orchestrator.py", "handlers.py", "state_machine.py", "auto_merge.py"):
            assert any(f"chain/{name}" in k for k in files), name

    def test_unknown_area_loads_handlers_py(self, tmp_path: Path) -> None:
        """chain/handlers.py must be loaded for proposed_area='unknown' — it
        was previously named in the file listing but never actually
        pre-loaded, so an "unknown" dispatch-code concern got no chance at a
        correct diagnosis."""
        factory_dir = tmp_path / "factory"
        (factory_dir / "chain").mkdir(parents=True)
        (factory_dir / "chain" / "handlers.py").write_text("# handlers\n", encoding="utf-8")
        (factory_dir / "chain" / "orchestrator.py").write_text("# orchestrator\n", encoding="utf-8")
        (factory_dir / "chain" / "auto_merge.py").write_text("# auto_merge\n", encoding="utf-8")
        (factory_dir / "runner.py").write_text("# runner\n", encoding="utf-8")

        files = _pre_load_source("unknown", factory_dir=factory_dir, root=tmp_path)

        assert any("chain/handlers.py" in k for k in files)

    def test_total_bundle_cap_enforced(self) -> None:
        """The whole bundle must stay within _BUNDLE_TOTAL_CAP, not just warn.

        prompt_edit loads every persona, which historically pushed the bundle
        to ~147KB; the diagnostician then silently received an over-budget
        context. The cap must now be enforced by trimming.
        """
        from factory.manager.diagnostician import _BUNDLE_TOTAL_CAP

        files = _pre_load_source("prompt_edit", factory_dir=self._factory_dir)
        total = sum(len(v) for v in files.values())
        # Allow a small per-file overage for the appended truncation-notice strings.
        assert total <= _BUNDLE_TOTAL_CAP + 200 * len(files)
        # Sanity: trimming kept every file represented, none dropped.
        assert len(files) >= 5


# ---------------------------------------------------------------------------
# Tests: invalid JSON response → sentinel escalation
# ---------------------------------------------------------------------------


class TestInvalidJsonResponseReturnsSentinelEscalation:
    """LLM returns garbage → sentinel proposal with target_class='escalate_to_human'."""

    def test_garbage_response_returns_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_concern(tmp_path)

        call_count = 0

        def _garbage_llm(persona, prompt, model_id, schema=None, **kwargs):
            nonlocal call_count
            call_count += 1
            raise json.JSONDecodeError("bad json", "", 0)

        monkeypatch.setattr("factory.manager.diagnostician.text_run", _garbage_llm)
        monkeypatch.setattr(
            "factory.manager.diagnostician._read_persona_prompt", _mock_persona_prompt
        )
        import factory.model_router as mr
        monkeypatch.setattr(mr, "route", _mock_route)
        monkeypatch.setattr(mr, "max_output_tokens_for", _mock_max_tokens)

        result = run_diagnostician_once(root=tmp_path, now=NOW)

        assert result is not None
        assert result["target_class"] == "escalate_to_human"
        assert result["escalate_to_human"] is True
        assert result.get("escalation_reason") is not None

    def test_sentinel_proposal_file_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_concern(tmp_path)

        def _garbage_llm(persona, prompt, model_id, schema=None, **kwargs):
            raise json.JSONDecodeError("bad json", "", 0)

        monkeypatch.setattr("factory.manager.diagnostician.text_run", _garbage_llm)
        monkeypatch.setattr(
            "factory.manager.diagnostician._read_persona_prompt", _mock_persona_prompt
        )
        import factory.model_router as mr
        monkeypatch.setattr(mr, "route", _mock_route)
        monkeypatch.setattr(mr, "max_output_tokens_for", _mock_max_tokens)

        result = run_diagnostician_once(root=tmp_path, now=NOW)

        assert result is not None
        assert "proposal_path" in result
        assert Path(result["proposal_path"]).exists()


# ---------------------------------------------------------------------------
# Tests: skip already-processed concerns
# ---------------------------------------------------------------------------


class TestDiagnosticianSkipsAlreadyProcessedConcerns:
    """Concern with matching proposal → None."""

    def test_already_processed_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_concern(tmp_path)
        _write_existing_proposal(tmp_path, "sm-token-overflow-loop")

        llm_called = False

        def _tracking_llm(persona, prompt, model_id, **kwargs):
            nonlocal llm_called
            llm_called = True
            return _CANNED_PROPOSAL

        monkeypatch.setattr("factory.manager.diagnostician.text_run", _tracking_llm)
        monkeypatch.setattr(
            "factory.manager.diagnostician._read_persona_prompt", _mock_persona_prompt
        )
        import factory.model_router as mr
        monkeypatch.setattr(mr, "route", _mock_route)
        monkeypatch.setattr(mr, "max_output_tokens_for", _mock_max_tokens)

        result = run_diagnostician_once(root=tmp_path, now=NOW)

        assert result is None
        assert not llm_called


# ---------------------------------------------------------------------------
# Tests: dry-run
# ---------------------------------------------------------------------------


class TestDryRunDoesNotCallLlm:
    """Dry-run mode does not call LLM."""

    def test_dry_run_no_llm_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _write_concern(tmp_path)

        llm_called = False

        def _tracking_llm(persona, prompt, model_id, **kwargs):
            nonlocal llm_called
            llm_called = True
            return _CANNED_PROPOSAL

        monkeypatch.setattr("factory.manager.diagnostician.text_run", _tracking_llm)
        monkeypatch.setattr(
            "factory.manager.diagnostician._read_persona_prompt", _mock_persona_prompt
        )
        import factory.model_router as mr
        monkeypatch.setattr(mr, "route", _mock_route)
        monkeypatch.setattr(mr, "max_output_tokens_for", _mock_max_tokens)

        result = run_diagnostician_once(root=tmp_path, now=NOW, dry_run=True)

        assert not llm_called
        assert result is not None
        assert result["target_class"] == "escalate_to_human"

    def test_dry_run_prints_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _write_concern(tmp_path)
        monkeypatch.setattr("factory.manager.diagnostician.text_run", _make_mock_llm(_CANNED_PROPOSAL))
        monkeypatch.setattr(
            "factory.manager.diagnostician._read_persona_prompt", _mock_persona_prompt
        )
        import factory.model_router as mr
        monkeypatch.setattr(mr, "route", _mock_route)
        monkeypatch.setattr(mr, "max_output_tokens_for", _mock_max_tokens)

        run_diagnostician_once(root=tmp_path, now=NOW, dry_run=True)

        out = capsys.readouterr().out
        assert "sm-token-overflow-loop" in out


# ---------------------------------------------------------------------------
# Tests: watcher daemon L3 trigger
# ---------------------------------------------------------------------------


class TestWatchDaemonTriggersL3OnL2Escalation:
    """Watcher daemon triggers L3 when L2 returns escalate_to_l3=true."""

    def test_l3_triggered_on_l2_escalation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build a concern path for L2 to return.
        concern_path = _write_concern(tmp_path)

        # Mock L1 watcher to return escalate_to_l2=true.
        from factory.manager import watcher as watcher_mod

        def _mock_watcher_once(root, lookback=None, **kwargs):
            return {
                "ts": NOW.isoformat(),
                "schema_version": 1,
                "event": "watcher_notes",
                "lookback_minutes": 15.0,
                "since_ts": (NOW - timedelta(minutes=15)).isoformat(),
                "note": {
                    "summary": "SM overflow detected",
                    "escalate_to_l2": True,
                    "escalation_reason": "SM overflow",
                    "observations": [],
                },
            }

        monkeypatch.setattr(watcher_mod, "run_watcher_once", _mock_watcher_once)

        # Mock L2 summarizer to return a concern with escalate_to_l3=true.
        import factory.manager.summarizer as summarizer_mod

        def _mock_summarizer_once(root=None, **kwargs):
            concern = dict(_CANNED_CONCERN)
            concern["concern_path"] = str(concern_path)
            return concern

        monkeypatch.setattr(summarizer_mod, "run_summarizer_once", _mock_summarizer_once)

        # Mock L3 diagnostician to capture calls.
        import factory.manager.diagnostician as diag_mod

        l3_called_with: list[dict] = []

        def _mock_diagnostician_once(root=None, concern_path=None, **kwargs):
            l3_called_with.append({"root": str(root), "concern_path": str(concern_path)})
            return dict(_CANNED_PROPOSAL, proposal_path=str(tmp_path / "state/manager_proposals/x.json"))

        monkeypatch.setattr(diag_mod, "run_diagnostician_once", _mock_diagnostician_once)

        # Run one daemon iteration.
        run_watcher_daemon(
            root=tmp_path,
            max_iters=1,
            interval_s=0,
            trigger_l2=True,
            trigger_l3=True,
        )

        assert len(l3_called_with) == 1
        assert l3_called_with[0]["concern_path"] == str(concern_path)

    def test_l3_not_triggered_when_l2_does_not_escalate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from factory.manager import watcher as watcher_mod

        def _mock_watcher_once(root, lookback=None, **kwargs):
            return {
                "ts": NOW.isoformat(),
                "schema_version": 1,
                "event": "watcher_notes",
                "lookback_minutes": 15.0,
                "since_ts": (NOW - timedelta(minutes=15)).isoformat(),
                "note": {
                    "summary": "All quiet",
                    "escalate_to_l2": True,
                    "escalation_reason": "routine",
                    "observations": [],
                },
            }

        monkeypatch.setattr(watcher_mod, "run_watcher_once", _mock_watcher_once)

        import factory.manager.summarizer as summarizer_mod

        def _mock_summarizer_once(root=None, **kwargs):
            concern = dict(_CANNED_CONCERN, escalate_to_l3=False, escalation_reason=None)
            concern["concern_path"] = str(tmp_path / "state/concerns/no-escalate.json")
            return concern

        monkeypatch.setattr(summarizer_mod, "run_summarizer_once", _mock_summarizer_once)

        import factory.manager.diagnostician as diag_mod

        l3_called = False

        def _mock_diagnostician_once(root=None, concern_path=None, **kwargs):
            nonlocal l3_called
            l3_called = True
            return None

        monkeypatch.setattr(diag_mod, "run_diagnostician_once", _mock_diagnostician_once)

        run_watcher_daemon(
            root=tmp_path,
            max_iters=1,
            interval_s=0,
            trigger_l2=True,
            trigger_l3=True,
        )

        assert not l3_called


# ---------------------------------------------------------------------------
# Tests: --no-l3 flag suppresses L3
# ---------------------------------------------------------------------------


class TestNoL3FlagSuppressesL3:
    """--no-l3 (trigger_l3=False) suppresses L3 trigger."""

    def test_no_l3_suppresses_l3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        concern_path = _write_concern(tmp_path)

        from factory.manager import watcher as watcher_mod

        def _mock_watcher_once(root, lookback=None, **kwargs):
            return {
                "ts": NOW.isoformat(),
                "schema_version": 1,
                "event": "watcher_notes",
                "lookback_minutes": 15.0,
                "since_ts": (NOW - timedelta(minutes=15)).isoformat(),
                "note": {
                    "summary": "SM overflow detected",
                    "escalate_to_l2": True,
                    "escalation_reason": "SM overflow",
                    "observations": [],
                },
            }

        monkeypatch.setattr(watcher_mod, "run_watcher_once", _mock_watcher_once)

        import factory.manager.summarizer as summarizer_mod

        def _mock_summarizer_once(root=None, **kwargs):
            concern = dict(_CANNED_CONCERN)
            concern["concern_path"] = str(concern_path)
            return concern

        monkeypatch.setattr(summarizer_mod, "run_summarizer_once", _mock_summarizer_once)

        import factory.manager.diagnostician as diag_mod

        l3_called = False

        def _mock_diagnostician_once(root=None, concern_path=None, **kwargs):
            nonlocal l3_called
            l3_called = True
            return None

        monkeypatch.setattr(diag_mod, "run_diagnostician_once", _mock_diagnostician_once)

        run_watcher_daemon(
            root=tmp_path,
            max_iters=1,
            interval_s=0,
            trigger_l2=True,
            trigger_l3=False,  # <--- suppress L3
        )

        assert not l3_called


# ---------------------------------------------------------------------------
# MVP acceptance test
# ---------------------------------------------------------------------------


class TestSmOverflowConcernProducesPersonaSettingsProposal:
    """MVP: SM-overflow concern → persona_settings proposal with valid unified diff."""

    def test_sm_overflow_produces_proposal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # (a) Plant SM-overflow concern.
        _write_concern(tmp_path)

        # Track LLM calls.
        captured_prompts: list[str] = []
        monkeypatch.setattr(
            "factory.manager.diagnostician.text_run",
            _make_capturing_llm(_CANNED_PROPOSAL, captured_prompts),
        )
        monkeypatch.setattr(
            "factory.manager.diagnostician._read_persona_prompt",
            _mock_persona_prompt,
        )
        import factory.model_router as mr
        monkeypatch.setattr(mr, "route", _mock_route)
        monkeypatch.setattr(mr, "max_output_tokens_for", _mock_max_tokens)

        result = run_diagnostician_once(root=tmp_path, now=NOW)

        # (a) L3 was called.
        assert len(captured_prompts) == 1, "L3 LLM must have been called exactly once"

        # (b) Prompt contains concern diagnosis text.
        prompt = captured_prompts[0]
        assert "max_tokens=65536" in prompt, "Prompt must contain the concern's description text"

        # (c) Prompt contains at least one source file.
        # For proposed_area=persona_settings, routes.yaml is included.
        assert "routes.yaml" in prompt or ".md" in prompt, (
            "Prompt must contain at least one pre-loaded source file"
        )

        # (d) Proposal file was written under state/manager_proposals/.
        assert result is not None
        assert "proposal_path" in result
        proposal_path = Path(result["proposal_path"])
        assert proposal_path.parent == _proposals_dir(tmp_path), (
            "Proposal must be under state/manager_proposals/"
        )
        assert proposal_path.exists(), "Proposal file must exist on disk"

        # (e) target_class=persona_settings and non-empty suggested_patch.
        assert result["target_class"] == "persona_settings"
        patch = result["proposal"]["suggested_patch"]
        assert patch, "suggested_patch must be non-empty"

        # (f) Patch parses as a unified diff — check for @@ hunks + --- / +++ headers.
        assert re.search(r"^@@.*@@", patch, re.MULTILINE), (
            "suggested_patch must contain @@ hunk markers"
        )
        assert re.search(r"^--- ", patch, re.MULTILINE), (
            "suggested_patch must contain --- header"
        )
        assert re.search(r"^\+\+\+ ", patch, re.MULTILINE), (
            "suggested_patch must contain +++ header"
        )

    def test_proposal_concern_title_echoes_concern(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """concern_title in proposal must match the concern's title."""
        _write_concern(tmp_path)
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL)

        result = run_diagnostician_once(root=tmp_path, now=NOW)

        assert result is not None
        assert result["concern_title"] == _CANNED_CONCERN["title"]

    def test_specific_concern_path_bypasses_unprocessed_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When concern_path is given directly, bypass the unprocessed check."""
        concern_path = _write_concern(tmp_path)
        # Also write an existing proposal (would normally cause it to be skipped).
        _write_existing_proposal(tmp_path, "sm-token-overflow-loop")

        # With concern_path given explicitly, it should NOT be skipped.
        _patch_llm_infra(monkeypatch, _CANNED_PROPOSAL)

        result = run_diagnostician_once(root=tmp_path, concern_path=concern_path, now=NOW)

        # Should still produce a result (bypasses the "already processed" check).
        assert result is not None
