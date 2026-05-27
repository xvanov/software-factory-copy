"""Tests for factory.manager.summarizer — L2 Summarizer agent (Phase 4).

All LLM calls are mocked; tests are deterministic.

Test inventory
--------------
test_no_flagged_notes_returns_none
    When no escalate_to_l2=true notes exist since lookback, returns None
    without making an LLM call.

test_flagged_note_triggers_l2
    Write a flagged watcher note + matching signals, run summarizer once
    with mocked LLM, confirm a concern file is written + concerns.ndjson
    line appended.

test_l2_prompt_contains_flagged_notes_and_signals
    Mock LLM as a passthrough, capture the prompt, assert it contains the
    flagged note's summary AND specific events from the underlying signals.

test_l2_handles_invalid_json_response
    Sentinel returned, no exception.

test_l2_continuity_prior_concerns_in_prompt
    Write 2 prior concerns to state/concerns/, run summarizer, assert
    prompt mentions them.

test_watch_daemon_triggers_immediate_l2_on_l1_escalation
    Mock both LLMs, run run_watcher_daemon for 1 iteration with an L1
    mock returning escalate_to_l2=true, confirm L2 was invoked.

test_watch_daemon_no_l2_flag_suppresses_l2
    Same setup but with trigger_l2=False; confirm L2 NOT called.

test_dry_run_does_not_call_llm
    For summarizer.

test_sm_overflow_synthetic_produces_concern_with_warn_urgency (MVP)
    Re-use the Phase 3 synthetic fixture (3 SM failures). Add a
    corresponding flagged watcher note. Mock L2's LLM to return a concern
    with urgency="warn", escalate_to_l3=True, evidence pointing at the
    3 runs. Assert the concern file is written, the concerns.ndjson line
    is appended, and the evidence list references the 3 specific run IDs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from factory.manager.summarizer import (
    _build_user_message,
    _concerns_dir,
    _events_path,
    _read_prior_concerns,
    run_summarizer_once,
)
from factory.manager.watcher import run_watcher_daemon

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=2)

# A canonical canned concern from the LLM.
_CANNED_CONCERN = {
    "title": "sm-token-overflow-loop",
    "description": (
        "Three consecutive SM persona calls failed with json parse failed at max_tokens=65536. "
        "The pattern began approximately 10 minutes ago and has affected stories 100, 101, and 102. "
        "Each failure results in a story rollback to story_created and a retry, burning approximately "
        "$1.73 per attempt. The SM persona appears to be generating responses that exceed its output "
        "token limit on these stories, likely due to prompt construction or persona output verbosity. "
        "No self-resolution is apparent from the available evidence."
    ),
    "evidence": [
        {"kind": "run", "id": 100, "ts": "2026-05-26T11:51:00+00:00", "excerpt": "sm failure max_tokens=65536"},
        {"kind": "run", "id": 101, "ts": "2026-05-26T11:53:00+00:00", "excerpt": "sm failure max_tokens=65536"},
        {"kind": "run", "id": 102, "ts": "2026-05-26T11:55:00+00:00", "excerpt": "sm failure max_tokens=65536"},
        {"kind": "watcher_note", "ts": "2026-05-26T11:57:00+00:00", "summary_excerpt": "SM overflow detected"},
    ],
    "proposed_area": "prompt",
    "urgency": "warn",
    "escalate_to_l3": True,
    "escalation_reason": "Repeated SM token-overflow failures across 3 distinct stories, no resolution.",
}

# A sentinel-like concern the LLM returns for the "no coherent signal" case.
_CONTINUE_CONCERN = {
    "title": "no-coherent-signal",
    "description": "no coherent signal — watcher note was escalated but underlying signals are inconclusive.",
    "evidence": [],
    "proposed_area": "unknown",
    "urgency": "continue",
    "escalate_to_l3": False,
    "escalation_reason": None,
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _write_run_event(
    root: Path,
    *,
    ts: str,
    success: bool,
    persona: str = "sm",
    story_id: int = 1,
    error: str | None = None,
    run_id: int | None = None,
) -> None:
    """Append a run event to state/events/runs.ndjson."""
    path = root / "state" / "events" / "runs.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {
        "ts": ts,
        "schema_version": 1,
        "event": "run",
        "success": success,
        "persona": persona,
        "story_id": story_id,
        "cost_usd": 1.73,
        "error": error,
        "model": "azure/gpt-5.4",
        "model_tier": None,
        "tokens_in": 60000,
        "tokens_out": 65536,
        "duration_s": 30.0,
        "attempt_n": 1,
    }
    if run_id is not None:
        rec["id"] = run_id
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _write_watcher_note(
    root: Path,
    *,
    ts: str,
    summary: str,
    escalated: bool = False,
    escalation_reason: str | None = None,
    observations: list[dict] | None = None,
) -> None:
    """Append a watcher note to state/events/watcher_notes.ndjson."""
    path = root / "state" / "events" / "watcher_notes.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    since_dt = datetime.fromisoformat(ts) - timedelta(minutes=15)
    envelope = {
        "ts": ts,
        "schema_version": 1,
        "event": "watcher_notes",
        "lookback_minutes": 15.0,
        "since_ts": since_dt.isoformat(),
        "note": {
            "summary": summary,
            "escalate_to_l2": escalated,
            "escalation_reason": escalation_reason if escalated else None,
            "observations": observations or [],
        },
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(envelope) + "\n")


def _write_prior_concern(root: Path, *, slug: str, urgency: str = "warn") -> Path:
    """Write a concern JSON file to state/concerns/."""
    concerns_dir = root / "state" / "concerns"
    concerns_dir.mkdir(parents=True, exist_ok=True)
    concern = {
        "title": slug,
        "description": f"Prior concern: {slug}",
        "evidence": [],
        "proposed_area": "unknown",
        "urgency": urgency,
        "escalate_to_l3": False,
        "escalation_reason": None,
    }
    path = concerns_dir / f"20260526T000000-{slug}.json"
    path.write_text(json.dumps(concern, indent=2), encoding="utf-8")
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


def _make_capturing_llm(
    response: dict[str, Any], captured_prompts: list[str]
):
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoFlaggedNotesReturnsNone:
    """When no escalate_to_l2=true notes exist since lookback, returns None."""

    def test_no_notes_at_all(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        llm_called = False

        def _tracking_llm(persona, prompt, model_id, **kwargs):
            nonlocal llm_called
            llm_called = True
            return _CANNED_CONCERN

        monkeypatch.setattr("factory.manager.summarizer.text_run", _tracking_llm)
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        result = run_summarizer_once(root=tmp_path, now=NOW)

        assert result is None
        assert not llm_called

    def test_only_non_escalated_notes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Write notes that are NOT escalated.
        _write_watcher_note(
            tmp_path,
            ts=(NOW - timedelta(minutes=30)).isoformat(),
            summary="all quiet",
            escalated=False,
        )
        _write_watcher_note(
            tmp_path,
            ts=(NOW - timedelta(minutes=15)).isoformat(),
            summary="still quiet",
            escalated=False,
        )

        llm_called = False

        def _tracking_llm(persona, prompt, model_id, **kwargs):
            nonlocal llm_called
            llm_called = True
            return _CANNED_CONCERN

        monkeypatch.setattr("factory.manager.summarizer.text_run", _tracking_llm)
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        result = run_summarizer_once(root=tmp_path, now=NOW)

        assert result is None
        assert not llm_called

    def test_escalated_note_outside_lookback_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Write an escalated note that is older than the lookback.
        old_ts = (NOW - timedelta(hours=4)).isoformat()
        _write_watcher_note(
            tmp_path,
            ts=old_ts,
            summary="old escalation",
            escalated=True,
            escalation_reason="old reason",
        )

        monkeypatch.setattr(
            "factory.manager.summarizer.text_run",
            _make_mock_llm(_CANNED_CONCERN),
        )
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        # Lookback is 2 hours; the note is 4 hours old — should return None.
        result = run_summarizer_once(root=tmp_path, now=NOW, lookback=timedelta(hours=2))

        assert result is None


class TestFlaggedNoteTriggersL2:
    """Write flagged note + signals, confirm concern file + concerns.ndjson."""

    def test_concern_file_written(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_watcher_note(
            tmp_path,
            ts=(NOW - timedelta(minutes=5)).isoformat(),
            summary="SM overflow detected",
            escalated=True,
            escalation_reason="SM token overflow",
        )

        monkeypatch.setattr(
            "factory.manager.summarizer.text_run",
            _make_mock_llm(_CANNED_CONCERN),
        )
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        result = run_summarizer_once(root=tmp_path, now=NOW)

        assert result is not None
        assert "concern_path" in result
        concern_path = Path(result["concern_path"])
        assert concern_path.exists()

        written = json.loads(concern_path.read_text())
        assert written["title"] == _CANNED_CONCERN["title"]
        assert written["urgency"] == _CANNED_CONCERN["urgency"]

    def test_concerns_ndjson_appended(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_watcher_note(
            tmp_path,
            ts=(NOW - timedelta(minutes=5)).isoformat(),
            summary="SM overflow detected",
            escalated=True,
            escalation_reason="SM token overflow",
        )

        monkeypatch.setattr(
            "factory.manager.summarizer.text_run",
            _make_mock_llm(_CANNED_CONCERN),
        )
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        run_summarizer_once(root=tmp_path, now=NOW)

        concerns_ndjson = _events_path(tmp_path, "concerns")
        assert concerns_ndjson.exists()
        lines = [ln for ln in concerns_ndjson.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1

        event = json.loads(lines[0])
        assert event["event"] == "concern_emitted"
        assert event["schema_version"] == 1
        assert event["title"] == _CANNED_CONCERN["title"]
        assert event["urgency"] == _CANNED_CONCERN["urgency"]
        assert "concern_path" in event

    def test_return_value_contains_concern_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_watcher_note(
            tmp_path,
            ts=(NOW - timedelta(minutes=5)).isoformat(),
            summary="SM overflow detected",
            escalated=True,
            escalation_reason="SM token overflow",
        )

        monkeypatch.setattr(
            "factory.manager.summarizer.text_run",
            _make_mock_llm(_CANNED_CONCERN),
        )
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        result = run_summarizer_once(root=tmp_path, now=NOW)

        assert result is not None
        assert result["title"] == _CANNED_CONCERN["title"]
        assert result["urgency"] == "warn"
        assert result["escalate_to_l3"] is True


class TestL2PromptContainsFlaggedNotesAndSignals:
    """Mock LLM captures prompt; assert it contains flagged note + signals."""

    def test_prompt_contains_flagged_note_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        note_ts = (NOW - timedelta(minutes=5)).isoformat()
        _write_watcher_note(
            tmp_path,
            ts=note_ts,
            summary="UNIQUE_SUMMARY_MARKER_FOR_SM_OVERFLOW",
            escalated=True,
            escalation_reason="SM token overflow repeated",
        )

        captured: list[str] = []
        monkeypatch.setattr(
            "factory.manager.summarizer.text_run",
            _make_capturing_llm(_CANNED_CONCERN, captured),
        )
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        run_summarizer_once(root=tmp_path, now=NOW)

        assert len(captured) == 1
        prompt = captured[0]
        assert "UNIQUE_SUMMARY_MARKER_FOR_SM_OVERFLOW" in prompt
        assert "SM token overflow repeated" in prompt

    def test_prompt_contains_underlying_signals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        note_ts = (NOW - timedelta(minutes=5)).isoformat()
        _write_watcher_note(
            tmp_path,
            ts=note_ts,
            summary="SM overflow",
            escalated=True,
            escalation_reason="SM token overflow",
        )
        # Write a run event in the window.
        _write_run_event(
            tmp_path,
            ts=(NOW - timedelta(minutes=10)).isoformat(),
            success=False,
            persona="sm",
            story_id=42,
            error="UNIQUE_ERROR_STRING_json_parse_failed_max_tokens",
        )

        captured: list[str] = []
        monkeypatch.setattr(
            "factory.manager.summarizer.text_run",
            _make_capturing_llm(_CANNED_CONCERN, captured),
        )
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        run_summarizer_once(root=tmp_path, now=NOW)

        assert len(captured) == 1
        prompt = captured[0]
        assert "UNIQUE_ERROR_STRING_json_parse_failed_max_tokens" in prompt

    def test_prompt_contains_detector_docstrings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_watcher_note(
            tmp_path,
            ts=(NOW - timedelta(minutes=5)).isoformat(),
            summary="SM overflow",
            escalated=True,
            escalation_reason="SM token overflow",
        )

        captured: list[str] = []
        monkeypatch.setattr(
            "factory.manager.summarizer.text_run",
            _make_capturing_llm(_CANNED_CONCERN, captured),
        )
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        run_summarizer_once(root=tmp_path, now=NOW)

        prompt = captured[0]
        # All detector names must appear (docstrings are injected).
        for name in ("runs_failed_since", "retry_storm", "cost_spike",
                     "tick_duration_outliers", "state_distribution_skew", "worktree_orphans"):
            assert name in prompt, f"Detector '{name}' missing from L2 prompt"


class TestL2HandlesInvalidJsonResponse:
    """Sentinel concern returned; no exception raised."""

    def test_sentinel_on_runtime_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_watcher_note(
            tmp_path,
            ts=(NOW - timedelta(minutes=5)).isoformat(),
            summary="SM overflow",
            escalated=True,
            escalation_reason="SM token overflow",
        )

        def _bad_llm(persona, prompt, model_id, **kwargs):
            raise RuntimeError("LLM exploded with invalid JSON")

        monkeypatch.setattr("factory.manager.summarizer.text_run", _bad_llm)
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        # Should not raise.
        result = run_summarizer_once(root=tmp_path, now=NOW)

        assert result is not None
        assert result["title"] == "l2-parse-failure"
        assert result["urgency"] == "continue"
        assert result["escalate_to_l3"] is False
        assert "error" in result

    def test_sentinel_still_writes_concern_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_watcher_note(
            tmp_path,
            ts=(NOW - timedelta(minutes=5)).isoformat(),
            summary="SM overflow",
            escalated=True,
            escalation_reason="SM token overflow",
        )

        def _bad_llm(persona, prompt, model_id, **kwargs):
            raise RuntimeError("LLM exploded")

        monkeypatch.setattr("factory.manager.summarizer.text_run", _bad_llm)
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        result = run_summarizer_once(root=tmp_path, now=NOW)

        # Even a sentinel concern should be written to disk.
        assert result is not None
        assert "concern_path" in result
        concern_path = Path(result["concern_path"])
        assert concern_path.exists()


class TestL2ContinuityPriorConcernsInPrompt:
    """Write 2 prior concerns; run summarizer; assert prompt mentions them."""

    def test_prior_concerns_appear_in_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Write 2 prior concern files.
        _write_prior_concern(tmp_path, slug="prior-concern-alpha", urgency="warn")
        _write_prior_concern(tmp_path, slug="prior-concern-beta", urgency="continue")

        _write_watcher_note(
            tmp_path,
            ts=(NOW - timedelta(minutes=5)).isoformat(),
            summary="SM overflow again",
            escalated=True,
            escalation_reason="Repeated SM overflow",
        )

        captured: list[str] = []
        monkeypatch.setattr(
            "factory.manager.summarizer.text_run",
            _make_capturing_llm(_CANNED_CONCERN, captured),
        )
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        run_summarizer_once(root=tmp_path, now=NOW)

        assert len(captured) == 1
        prompt = captured[0]
        assert "prior-concern-alpha" in prompt
        assert "prior-concern-beta" in prompt

    def test_read_prior_concerns_returns_correct_count(self, tmp_path: Path) -> None:
        for i in range(7):
            _write_prior_concern(tmp_path, slug=f"concern-{i:02d}")
        concerns = _read_prior_concerns(tmp_path, limit=5)
        # Should return at most 5 (the most recent 5).
        assert len(concerns) == 5

    def test_read_prior_concerns_missing_dir(self, tmp_path: Path) -> None:
        # No state/concerns/ dir at all.
        concerns = _read_prior_concerns(tmp_path)
        assert concerns == []


class TestWatchDaemonTriggersImmediateL2OnL1Escalation:
    """Mock both LLMs; run watcher daemon 1 iteration with L1 escalation; confirm L2 called."""

    def test_l2_invoked_on_escalation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        l1_response = {
            "summary": "SM overflow escalated",
            "escalate_to_l2": True,
            "escalation_reason": "SM token overflow",
            "observations": [],
        }

        l2_called = False
        l2_result = {**_CANNED_CONCERN}

        def _l2_mock(*args, **kwargs):
            nonlocal l2_called
            l2_called = True
            return l2_result

        # Monkeypatch watcher's text_run (L1 LLM).
        monkeypatch.setattr(
            "factory.manager.watcher.text_run",
            _make_mock_llm(l1_response),
        )
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# L1 persona mock",
        )
        # Monkeypatch summarizer's text_run (L2 LLM).
        monkeypatch.setattr(
            "factory.manager.summarizer.text_run",
            _l2_mock,
        )
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        # Run daemon for 1 iteration (max_iters=1).
        run_watcher_daemon(
            root=tmp_path,
            interval_s=0,
            max_iters=1,
            trigger_l2=True,
        )

        assert l2_called, "L2 summarizer was NOT called even though L1 escalated"


class TestWatchDaemonNoL2FlagSuppressesL2:
    """Same setup as above but trigger_l2=False; confirm L2 NOT called."""

    def test_l2_suppressed_with_no_l2_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        l1_response = {
            "summary": "SM overflow escalated",
            "escalate_to_l2": True,
            "escalation_reason": "SM token overflow",
            "observations": [],
        }

        l2_called = False

        def _l2_mock(*args, **kwargs):
            nonlocal l2_called
            l2_called = True
            return _CANNED_CONCERN

        monkeypatch.setattr(
            "factory.manager.watcher.text_run",
            _make_mock_llm(l1_response),
        )
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# L1 persona mock",
        )
        monkeypatch.setattr(
            "factory.manager.summarizer.text_run",
            _l2_mock,
        )
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        run_watcher_daemon(
            root=tmp_path,
            interval_s=0,
            max_iters=1,
            trigger_l2=False,  # --no-l2
        )

        assert not l2_called, "L2 was called even though trigger_l2=False"


class TestDryRunDoesNotCallLlm:
    """Summarizer dry-run does not call the LLM."""

    def test_llm_not_called_in_dry_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_watcher_note(
            tmp_path,
            ts=(NOW - timedelta(minutes=5)).isoformat(),
            summary="SM overflow",
            escalated=True,
            escalation_reason="SM token overflow",
        )

        llm_called = False

        def _tracking_llm(persona, prompt, model_id, **kwargs):
            nonlocal llm_called
            llm_called = True
            return _CANNED_CONCERN

        monkeypatch.setattr("factory.manager.summarizer.text_run", _tracking_llm)
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        result = run_summarizer_once(root=tmp_path, now=NOW, dry_run=True)

        assert not llm_called
        assert result is not None
        assert result["title"] == "dry-run-sentinel"
        assert result["urgency"] == "continue"

    def test_dry_run_still_writes_concern(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_watcher_note(
            tmp_path,
            ts=(NOW - timedelta(minutes=5)).isoformat(),
            summary="SM overflow",
            escalated=True,
            escalation_reason="SM token overflow",
        )

        monkeypatch.setattr(
            "factory.manager.summarizer.text_run",
            _make_mock_llm(_CANNED_CONCERN),
        )
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        run_summarizer_once(root=tmp_path, now=NOW, dry_run=True)

        concerns_dir = _concerns_dir(tmp_path)
        assert concerns_dir.exists()
        files = list(concerns_dir.glob("*.json"))
        assert len(files) == 1


class TestSmOverflowSyntheticProducesConcernWithWarnUrgency:
    """MVP acceptance test: SM token-overflow synthetic fixture → L2 concern.

    Re-uses the Phase 3 synthetic fixture (3 SM failures). Adds a
    corresponding flagged watcher note. Mocks L2's LLM to return a concern
    with urgency="warn", escalate_to_l3=True, evidence pointing at the 3
    runs. Asserts:
    - the concern file is written;
    - the concerns.ndjson line is appended;
    - the evidence list references the 3 specific run IDs.
    """

    _RUN_IDS = [100, 101, 102]
    _SM_ERROR = "json parse failed at max_tokens=65536 finish_reason=length"

    def _inject_sm_failures(self, root: Path) -> list[str]:
        """Write 3 SM persona failures. Returns the ts strings used."""
        ts_list: list[str] = []
        base_ts = NOW - timedelta(minutes=15)
        for i, run_id in enumerate(self._RUN_IDS):
            ts = (base_ts + timedelta(minutes=i * 2)).isoformat()
            ts_list.append(ts)
            _write_run_event(
                root,
                ts=ts,
                success=False,
                persona="sm",
                story_id=run_id,
                error=self._SM_ERROR,
                run_id=run_id,
            )
        return ts_list

    def _inject_flagged_watcher_note(self, root: Path) -> None:
        """Write a watcher note escalated for the SM overflow pattern."""
        _write_watcher_note(
            root,
            ts=(NOW - timedelta(minutes=5)).isoformat(),
            summary=(
                "Three SM persona calls failed with json parse failed at "
                "max_tokens=65536. Pattern matches the SM token-overflow."
            ),
            escalated=True,
            escalation_reason="Repeated SM token-overflow (3 distinct stories).",
            observations=[
                {
                    "detector": "runs_failed_since",
                    "noteworthy": "3 SM failures with max_tokens=65536 in window",
                }
            ],
        )

    def _make_l2_response(self) -> dict[str, Any]:
        """Build the canned L2 response referencing the 3 run IDs."""
        return {
            "title": "sm-token-overflow-loop",
            "description": (
                "Three SM persona calls failed in the last 15 minutes, all with "
                "json parse failed at max_tokens=65536. "
                "This matches the SM token-overflow pattern that previously burned $12. "
                "Stories 100, 101, and 102 are affected. "
                "No resolution is visible in the available signals."
            ),
            "evidence": [
                {
                    "kind": "run",
                    "id": 100,
                    "ts": (NOW - timedelta(minutes=15)).isoformat(),
                    "excerpt": "sm failure max_tokens=65536 story_id=100",
                },
                {
                    "kind": "run",
                    "id": 101,
                    "ts": (NOW - timedelta(minutes=13)).isoformat(),
                    "excerpt": "sm failure max_tokens=65536 story_id=101",
                },
                {
                    "kind": "run",
                    "id": 102,
                    "ts": (NOW - timedelta(minutes=11)).isoformat(),
                    "excerpt": "sm failure max_tokens=65536 story_id=102",
                },
                {
                    "kind": "watcher_note",
                    "ts": (NOW - timedelta(minutes=5)).isoformat(),
                    "summary_excerpt": "SM overflow detected",
                },
            ],
            "proposed_area": "prompt",
            "urgency": "warn",
            "escalate_to_l3": True,
            "escalation_reason": (
                "Repeated SM token-overflow failures across 3 distinct stories, "
                "no sign of resolution. L3 judgment needed to propose a fix."
            ),
        }

    def _setup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[dict, list[str]]:
        """Shared setup: inject fixtures, install capturing LLM, run summarizer.

        Returns (result, captured_prompts).
        """
        self._inject_sm_failures(tmp_path)
        self._inject_flagged_watcher_note(tmp_path)

        captured: list[str] = []
        monkeypatch.setattr(
            "factory.manager.summarizer.text_run",
            _make_capturing_llm(self._make_l2_response(), captured),
        )
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        result = run_summarizer_once(root=tmp_path, now=NOW)
        return result, captured  # type: ignore[return-value]

    def test_concern_written_with_warn_urgency(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, _captured = self._setup(tmp_path, monkeypatch)

        assert result is not None
        assert result["urgency"] == "warn"
        assert result["escalate_to_l3"] is True

    def test_concern_file_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, _captured = self._setup(tmp_path, monkeypatch)

        assert result is not None
        concern_path = Path(result["concern_path"])
        assert concern_path.exists()

        written = json.loads(concern_path.read_text())
        assert written["urgency"] == "warn"
        assert written["escalate_to_l3"] is True

    def test_concerns_ndjson_appended(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _result, _captured = self._setup(tmp_path, monkeypatch)

        ndjson = _events_path(tmp_path, "concerns")
        assert ndjson.exists()
        lines = [ln for ln in ndjson.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1

        event = json.loads(lines[0])
        assert event["event"] == "concern_emitted"
        assert event["urgency"] == "warn"
        assert event["escalate_to_l3"] is True

    def test_evidence_references_three_run_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, _captured = self._setup(tmp_path, monkeypatch)

        assert result is not None
        evidence = result.get("evidence", [])
        run_evidence_ids = {
            ev["id"] for ev in evidence if ev.get("kind") == "run" and ev.get("id") is not None
        }
        for expected_id in self._RUN_IDS:
            assert expected_id in run_evidence_ids, (
                f"Run ID {expected_id} not found in evidence: {evidence}"
            )

    def test_prompt_contains_underlying_signals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The L2 prompt must faithfully forward the planted signals.

        This is the load-bearing regression guard: if a future refactor drops
        the underlying-signals section from the prompt, this test must fail.
        """
        _result, captured = self._setup(tmp_path, monkeypatch)

        assert len(captured) == 1, "LLM should have been called exactly once"
        prompt = captured[0]

        # Watcher note content forwarded to the LLM.
        assert "max_tokens=65536" in prompt, (
            "Prompt must contain the SM-overflow watcher note's escalation_reason / summary text"
        )

        # All three planted run IDs must appear in the prompt.
        for run_id in self._RUN_IDS:
            assert str(run_id) in prompt, (
                f"Prompt must contain planted run ID {run_id}; "
                "a refactor may have dropped the underlying-signals section"
            )

        # The exact error excerpt from the failure events must be in the prompt.
        assert self._SM_ERROR in prompt, (
            f"Prompt must contain the error excerpt '{self._SM_ERROR}' "
            "from the planted SM failure events"
        )


# ---------------------------------------------------------------------------
# schema_version in concern file
# ---------------------------------------------------------------------------


class TestConcernFileHasSchemaVersion:
    """Concern JSON files written to disk must carry schema_version=1."""

    def test_concern_file_has_schema_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_watcher_note(
            tmp_path,
            ts=(NOW - timedelta(minutes=5)).isoformat(),
            summary="SM overflow",
            escalated=True,
            escalation_reason="SM token overflow",
        )

        monkeypatch.setattr(
            "factory.manager.summarizer.text_run",
            _make_mock_llm(_CANNED_CONCERN),
        )
        monkeypatch.setattr(
            "factory.manager.summarizer._read_persona_prompt",
            lambda persona: "# L2 persona mock",
        )

        result = run_summarizer_once(root=tmp_path, now=NOW)

        assert result is not None
        concern_path = Path(result["concern_path"])
        assert concern_path.exists()

        written = json.loads(concern_path.read_text())
        assert written["schema_version"] == 1, (
            f"Concern file must have schema_version=1; got: {written.get('schema_version')!r}"
        )


# ---------------------------------------------------------------------------
# Unit tests for internal helpers
# ---------------------------------------------------------------------------


class TestBuildUserMessage:
    """Unit tests for the L2 prompt assembly."""

    def test_contains_persona_prompt(self) -> None:
        msg = _build_user_message(
            persona_prompt="# TEST PERSONA PROMPT",
            since=SINCE,
            now=NOW,
            flagged_notes=[],
            signals_by_window=[],
            prior_concerns=[],
        )
        assert "# TEST PERSONA PROMPT" in msg

    def test_contains_timing_metadata(self) -> None:
        msg = _build_user_message(
            persona_prompt="# persona",
            since=SINCE,
            now=NOW,
            flagged_notes=[],
            signals_by_window=[],
            prior_concerns=[],
        )
        assert "since_ts" in msg
        assert "now_ts" in msg
        assert "flagged_note_count" in msg

    def test_contains_prior_concerns(self) -> None:
        prior = [
            {
                "title": "test-prior-concern",
                "description": "a prior concern",
                "urgency": "warn",
                "evidence": [],
                "proposed_area": "unknown",
                "escalate_to_l3": False,
                "escalation_reason": None,
                "concern_path": "/some/path.json",
            }
        ]
        msg = _build_user_message(
            persona_prompt="# persona",
            since=SINCE,
            now=NOW,
            flagged_notes=[],
            signals_by_window=[],
            prior_concerns=prior,
        )
        assert "test-prior-concern" in msg

    def test_contains_flagged_note_summary(self) -> None:
        flagged = [
            {
                "ts": NOW.isoformat(),
                "since_ts": SINCE.isoformat(),
                "note": {
                    "summary": "UNIQUE_NOTE_SUMMARY_CONTENT",
                    "escalate_to_l2": True,
                    "escalation_reason": "test reason",
                    "observations": [],
                },
            }
        ]
        msg = _build_user_message(
            persona_prompt="# persona",
            since=SINCE,
            now=NOW,
            flagged_notes=flagged,
            signals_by_window=[],
            prior_concerns=[],
        )
        assert "UNIQUE_NOTE_SUMMARY_CONTENT" in msg
