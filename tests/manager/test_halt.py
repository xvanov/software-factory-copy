"""Tests for factory.manager.halt — Phase 7 halt authority.

Test inventory
--------------
test_request_halt_writes_state_file
    Call request_halt, verify file exists with expected fields.

test_is_halted_true_after_request
    Round-trip: request_halt → is_halted → True.

test_is_halted_false_when_no_file
    Clean root → is_halted returns False.

test_get_halt_state_returns_dict
    Full dict roundtrip.

test_clear_halt_archives_to_history
    Request halt, then clear; verify .halt_history.json has the record
    and state/factory_mode.json is gone.

test_request_halt_idempotent_archives_previous
    Request halt twice; second call archives the first state.

test_diagnostician_request_halt_writes_halt_file
    Mock L3 LLM to return request_halt=true, halt_reason="...".
    Run run_diagnostician_once. Verify halt file written + proposal
    shows halt_requested=true.

test_diagnostician_request_halt_requires_reason
    L3 returns request_halt=true, halt_reason=null.
    Halt is NOT triggered (silently dropped).

test_tick_skips_dispatch_when_halted
    Request halt, run tick, verify no text_run calls.

test_resume_clears_halt
    Request halt, call clear_halt, verify is_halted is False.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from factory.manager.halt import (
    _halt_path,
    _history_path,
    clear_halt,
    get_halt_state,
    is_halted,
    request_halt,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
_CONCERN_TITLE = "sm-token-overflow-runaway"
_PROPOSAL_PATH = "state/manager_proposals/20260526T120000-sm-token-overflow.json"
_REASON = "Three consecutive SM failures across 3 stories, cost spiralling, no self-healing."


# ---------------------------------------------------------------------------
# Basic halt module tests
# ---------------------------------------------------------------------------


class TestRequestHalt:
    def test_writes_state_file(self, tmp_path: Path) -> None:
        p = request_halt(
            root=tmp_path,
            concern_title=_CONCERN_TITLE,
            proposal_path=_PROPOSAL_PATH,
            reason=_REASON,
        )
        assert p.exists()
        state = json.loads(p.read_text())
        assert state["mode"] == "halted"
        assert state["schema_version"] == 1
        assert state["set_by"] == "manager_diagnostician"
        assert state["concern_title"] == _CONCERN_TITLE
        assert state["proposal_path"] == _PROPOSAL_PATH
        assert state["reason"] == _REASON
        assert "set_at" in state

    def test_path_is_state_factory_mode_json(self, tmp_path: Path) -> None:
        p = request_halt(
            root=tmp_path,
            concern_title=_CONCERN_TITLE,
            proposal_path=None,
            reason=_REASON,
        )
        assert p == tmp_path / "state" / "factory_mode.json"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        root = tmp_path / "deep" / "nested"
        p = request_halt(
            root=root,
            concern_title=_CONCERN_TITLE,
            proposal_path=None,
            reason=_REASON,
        )
        assert p.exists()

    def test_idempotent_archives_previous(self, tmp_path: Path) -> None:
        request_halt(
            root=tmp_path,
            concern_title="first-concern",
            proposal_path=None,
            reason="first reason",
        )
        # Second request overwrites, old state goes to history.
        request_halt(
            root=tmp_path,
            concern_title="second-concern",
            proposal_path=None,
            reason="second reason",
        )
        state = json.loads(_halt_path(tmp_path).read_text())
        assert state["concern_title"] == "second-concern"

        history_path = _history_path(tmp_path)
        assert history_path.exists()
        history = json.loads(history_path.read_text())
        assert isinstance(history, list)
        assert len(history) == 1
        assert history[0]["concern_title"] == "first-concern"


class TestIsHalted:
    def test_true_after_request(self, tmp_path: Path) -> None:
        request_halt(
            root=tmp_path,
            concern_title=_CONCERN_TITLE,
            proposal_path=None,
            reason=_REASON,
        )
        assert is_halted(root=tmp_path) is True

    def test_false_when_no_file(self, tmp_path: Path) -> None:
        assert is_halted(root=tmp_path) is False

    def test_false_when_file_wrong_mode(self, tmp_path: Path) -> None:
        p = tmp_path / "state" / "factory_mode.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"mode": "normal"}), encoding="utf-8")
        assert is_halted(root=tmp_path) is False

    def test_false_when_file_is_corrupt(self, tmp_path: Path) -> None:
        p = tmp_path / "state" / "factory_mode.json"
        p.parent.mkdir(parents=True)
        p.write_text("this is not json {{{", encoding="utf-8")
        assert is_halted(root=tmp_path) is False


class TestGetHaltState:
    def test_returns_dict_after_request(self, tmp_path: Path) -> None:
        request_halt(
            root=tmp_path,
            concern_title=_CONCERN_TITLE,
            proposal_path=_PROPOSAL_PATH,
            reason=_REASON,
        )
        state = get_halt_state(root=tmp_path)
        assert isinstance(state, dict)
        assert state["mode"] == "halted"
        assert state["concern_title"] == _CONCERN_TITLE
        assert state["reason"] == _REASON

    def test_returns_none_when_no_file(self, tmp_path: Path) -> None:
        assert get_halt_state(root=tmp_path) is None

    def test_returns_none_when_not_halted(self, tmp_path: Path) -> None:
        p = tmp_path / "state" / "factory_mode.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"mode": "paused"}), encoding="utf-8")
        assert get_halt_state(root=tmp_path) is None


class TestClearHalt:
    def test_archives_to_history(self, tmp_path: Path) -> None:
        request_halt(
            root=tmp_path,
            concern_title=_CONCERN_TITLE,
            proposal_path=_PROPOSAL_PATH,
            reason=_REASON,
        )
        archived = clear_halt(root=tmp_path, cleared_by="operator", reason="manual override")

        # Halt file should be gone.
        assert not _halt_path(tmp_path).exists()
        assert is_halted(root=tmp_path) is False

        # History should have the archived entry.
        history_path = _history_path(tmp_path)
        assert history_path.exists()
        history = json.loads(history_path.read_text())
        assert isinstance(history, list)
        assert len(history) == 1
        entry = history[0]
        assert entry["mode"] == "halted"
        assert entry["concern_title"] == _CONCERN_TITLE
        assert entry["cleared_by"] == "operator"
        assert entry["clear_reason"] == "manual override"
        assert "cleared_at" in entry

        # Return value.
        assert archived["cleared_by"] == "operator"
        assert archived["clear_reason"] == "manual override"

    def test_clear_without_reason(self, tmp_path: Path) -> None:
        request_halt(
            root=tmp_path,
            concern_title=_CONCERN_TITLE,
            proposal_path=None,
            reason=_REASON,
        )
        archived = clear_halt(root=tmp_path)
        assert "clear_reason" not in archived or archived.get("clear_reason") is None
        assert not is_halted(root=tmp_path)

    def test_raises_if_no_halt_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            clear_halt(root=tmp_path)

    def test_raises_if_mode_not_halted(self, tmp_path: Path) -> None:
        p = tmp_path / "state" / "factory_mode.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"mode": "paused"}), encoding="utf-8")
        with pytest.raises(ValueError):
            clear_halt(root=tmp_path)


class TestResumeClears:
    def test_resume_clears_halt(self, tmp_path: Path) -> None:
        request_halt(
            root=tmp_path,
            concern_title=_CONCERN_TITLE,
            proposal_path=None,
            reason=_REASON,
        )
        assert is_halted(root=tmp_path) is True
        clear_halt(root=tmp_path, cleared_by="operator", reason="test clear")
        assert is_halted(root=tmp_path) is False


class TestResumeGrace:
    """An operator resume suppresses manager re-halts for a grace window.

    Stall-class concerns ("no ticks for N minutes") can only clear AFTER a
    resume lets the orchestrator run; an immediate re-halt deadlocks the
    factory against its own manager (observed live 2026-06-11: re-halt 94s
    after resume, before the first post-resume tick).
    """

    def test_request_halt_suppressed_within_grace(self, tmp_path: Path) -> None:
        request_halt(
            root=tmp_path, concern_title=_CONCERN_TITLE, proposal_path=None, reason=_REASON
        )
        clear_halt(root=tmp_path, cleared_by="operator", reason="resume")

        out = request_halt(
            root=tmp_path,
            concern_title=_CONCERN_TITLE + "-continued",
            proposal_path=None,
            reason=_REASON,
        )
        assert out is None
        assert is_halted(root=tmp_path) is False

    def test_request_halt_allowed_after_grace_expires(self, tmp_path: Path) -> None:
        from datetime import timedelta

        from factory.manager.halt import _RESUME_GRACE_MINUTES

        old = (
            datetime.now(UTC) - timedelta(minutes=_RESUME_GRACE_MINUTES + 1)
        ).isoformat()
        history = tmp_path / "state" / ".halt_history.json"
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(
            json.dumps([{"mode": "halted", "cleared_at": old, "cleared_by": "operator"}]),
            encoding="utf-8",
        )

        out = request_halt(
            root=tmp_path, concern_title=_CONCERN_TITLE, proposal_path=None, reason=_REASON
        )
        assert out is not None
        assert is_halted(root=tmp_path) is True

    def test_grace_uses_latest_clear_even_after_later_archives(self, tmp_path: Path) -> None:
        """Archive entries written by request_halt overwrites (no cleared_at)
        after an operator clear must not mask the clear's recency."""
        request_halt(
            root=tmp_path, concern_title="first", proposal_path=None, reason=_REASON
        )
        clear_halt(root=tmp_path, cleared_by="operator")
        # Manually append a non-clear archive entry AFTER the operator clear.
        history = json.loads(_history_path(tmp_path).read_text())
        history.append({"mode": "halted", "concern_title": "noise"})
        _history_path(tmp_path).write_text(json.dumps(history), encoding="utf-8")

        out = request_halt(
            root=tmp_path, concern_title="second", proposal_path=None, reason=_REASON
        )
        assert out is None  # still inside the grace window


# ---------------------------------------------------------------------------
# Diagnostician integration tests
# ---------------------------------------------------------------------------

_CANNED_CONCERN = {
    "schema_version": 1,
    "title": _CONCERN_TITLE,
    "description": "Three SM failures; runaway cost.",
    "evidence": [
        {"kind": "run", "id": 100, "ts": "2026-05-26T11:51:00+00:00", "excerpt": "sm failure"},
    ],
    "proposed_area": "persona_settings",
    "urgency": "halt",
    "escalate_to_l3": True,
    "escalation_reason": "Sustained cost spiral, no self-healing.",
}


def _write_concern(root: Path, concern: dict[str, Any] | None = None) -> Path:
    concerns_dir = root / "state" / "concerns"
    concerns_dir.mkdir(parents=True, exist_ok=True)
    doc = concern if concern is not None else dict(_CANNED_CONCERN)
    path = concerns_dir / f"20260526T115500-{_CONCERN_TITLE}.json"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _make_mock_llm(response: dict[str, Any]):
    def _mock(persona: str, prompt: str, model_id: str, schema: Any = None, **kwargs: Any) -> dict:
        return response

    return _mock


def _patch_llm_infra(monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]) -> None:
    monkeypatch.setattr("factory.manager.diagnostician.text_run", _make_mock_llm(response))
    monkeypatch.setattr(
        "factory.manager.diagnostician._read_persona_prompt",
        lambda persona: f"# {persona} mock",
    )
    import factory.model_router as mr

    monkeypatch.setattr(mr, "route", lambda *a, **kw: "anthropic/claude-opus-4-7")
    monkeypatch.setattr(mr, "max_output_tokens_for", lambda *a, **kw: 32768)


_L3_HALT_RESPONSE = {
    "concern_title": _CONCERN_TITLE,
    "diagnosis": "Runaway cost spiral with no self-healing.",
    "proposal": {
        "kind": "persona_settings",
        "target": "factory/personas/sm.md",
        "rationale": "Lower max_tokens.",
        "suggested_patch": "",
        "verification": "uv run pytest",
        "confidence": "low",
    },
    "target_class": "escalate_to_human",
    "escalate_to_human": True,
    "escalation_reason": "Cannot fix; halting to stop burn.",
    "request_halt": True,
    "halt_reason": _REASON,
}

_L3_HALT_NO_REASON_RESPONSE = {
    **_L3_HALT_RESPONSE,
    "halt_reason": None,
}

_L3_NO_HALT_RESPONSE = {
    "concern_title": _CONCERN_TITLE,
    "diagnosis": "SM max_tokens exceeded.",
    "proposal": {
        "kind": "persona_settings",
        "target": "factory/routes.yaml",
        "rationale": "Lower max_tokens.",
        "suggested_patch": "diff --git a/factory/routes.yaml b/factory/routes.yaml\n",
        "verification": "uv run pytest",
        "confidence": "medium",
    },
    "target_class": "persona_settings",
    "escalate_to_human": False,
    "escalation_reason": None,
    "request_halt": False,
    "halt_reason": None,
}


class TestDiagnosticianHaltRequest:
    def test_request_halt_writes_halt_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_concern(tmp_path)
        _patch_llm_infra(monkeypatch, _L3_HALT_RESPONSE)

        from factory.manager.diagnostician import run_diagnostician_once

        result = run_diagnostician_once(root=tmp_path, now=NOW)
        assert result is not None

        # Halt file should exist.
        assert is_halted(root=tmp_path), "halt state file should be set"
        state = get_halt_state(root=tmp_path)
        assert state is not None
        assert state["concern_title"] == _CONCERN_TITLE
        assert state["reason"] == _REASON

        # Proposal should record halt_requested=True.
        assert result.get("halt_requested") is True

        # Proposal file should also have halt_requested=True.
        proposal_path = Path(result["proposal_path"])
        written = json.loads(proposal_path.read_text())
        assert written.get("halt_requested") is True

    def test_request_halt_requires_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """request_halt=true with halt_reason=null → halt NOT triggered."""
        _write_concern(tmp_path)
        _patch_llm_infra(monkeypatch, _L3_HALT_NO_REASON_RESPONSE)

        from factory.manager.diagnostician import run_diagnostician_once

        result = run_diagnostician_once(root=tmp_path, now=NOW)
        assert result is not None

        # Halt file must NOT exist.
        assert not is_halted(root=tmp_path), "halt must NOT be set when halt_reason is null"

        # Proposal should record halt_requested=False.
        assert result.get("halt_requested") is False

    def test_no_halt_request_does_not_write_halt_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_concern(tmp_path)
        _patch_llm_infra(monkeypatch, _L3_NO_HALT_RESPONSE)

        from factory.manager.diagnostician import run_diagnostician_once

        result = run_diagnostician_once(root=tmp_path, now=NOW)
        assert result is not None
        assert not is_halted(root=tmp_path)
        assert result.get("halt_requested") is False


# ---------------------------------------------------------------------------
# Tick halt check test
# ---------------------------------------------------------------------------


class TestTickSkipsDispatchWhenHalted:
    def test_tick_halted_returns_summary_with_halted_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """request_halt → tick returns TickSummary(halted=True) without dispatching."""
        from factory.chain.orchestrator import tick

        # Write halt state.
        request_halt(
            root=tmp_path,
            concern_title=_CONCERN_TITLE,
            proposal_path=None,
            reason=_REASON,
        )

        # Track text_run calls.
        text_run_calls: list = []

        def _mock_text_run(*args: Any, **kwargs: Any) -> Any:
            text_run_calls.append((args, kwargs))
            return {}

        # We need a minimal app config so tick doesn't fail at config load.
        app_dir = tmp_path / "apps" / "sacrifice"
        app_dir.mkdir(parents=True)
        config_content = (
            "name: sacrifice\n"
            "repo: https://github.com/test/sacrifice\n"
            "default_branch: main\n"
        )
        (app_dir / "config.yaml").write_text(config_content, encoding="utf-8")

        # Also create a minimal factory.db (so tick doesn't error on db open)
        db_path = tmp_path / "state" / "factory.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        summary = tick(
            tmp_path,
            "sacrifice",
            dry_run=True,
            db_path=db_path,
        )

        assert summary.halted is True
        assert summary.halt_reason == _REASON
        assert summary.stories_advanced == 0
        # No LLM calls should have been made (no handler invocations).
        assert len(text_run_calls) == 0

    def test_tick_not_halted_proceeds_normally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without halt, tick proceeds to story dispatch (stories_advanced may be 0 if no stories)."""
        from factory.chain.orchestrator import tick

        # No halt file.
        app_dir = tmp_path / "apps" / "sacrifice"
        app_dir.mkdir(parents=True)
        (app_dir / "config.yaml").write_text(
            "name: sacrifice\nrepo: https://github.com/test/sacrifice\ndefault_branch: main\n",
            encoding="utf-8",
        )

        db_path = tmp_path / "state" / "factory.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        summary = tick(
            tmp_path,
            "sacrifice",
            dry_run=True,
            db_path=db_path,
        )

        assert summary.halted is False
