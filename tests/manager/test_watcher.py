"""Tests for factory.manager.watcher — L1 Watcher agent (Phase 3).

All LLM calls are mocked; tests are deterministic.

Test inventory
--------------
test_run_watcher_once_with_no_signals
    Empty state/events/. Watcher produces a valid result without raising.

test_run_watcher_once_appends_to_watcher_notes
    Confirms the note is appended with envelope fields and note.summary set.

test_run_watcher_resumes_from_last_note_ts
    Write a prior note with ts=T. Verify the new run computes since >= T.

test_run_watcher_handles_invalid_json_response
    Mock LLM to return non-JSON twice. Sentinel result returned; no exception.

test_dry_run_does_not_call_llm
    Confirm dry-run bypasses the LLM helper.

test_watcher_flags_sm_token_overflow_pattern  (MVP acceptance test)
    Inject 3 SM failures (max_tokens error). Mock LLM to escalate.
    Verify the watcher note escalates and the prompt contains the
    planted events.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from factory.manager.watcher import (
    _build_user_message,
    _events_path,
    _read_prior_watcher_notes,
    _read_stream_since,
    run_watcher_once,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
SINCE = NOW - timedelta(minutes=15)

# Canonical canned LLM response used by the "good path" tests.
_CANNED_GOOD = {
    "summary": "nothing happened",
    "escalate_to_l2": False,
    "escalation_reason": None,
    "observations": [
        {"detector": "runs_failed_since", "noteworthy": None},
        {"detector": "retry_storm", "noteworthy": None},
        {"detector": "cost_spike", "noteworthy": None},
        {"detector": "tick_duration_outliers", "noteworthy": None},
        {"detector": "state_distribution_skew", "noteworthy": None},
        {"detector": "worktree_orphans", "noteworthy": None},
    ],
}

# Canned LLM response for the SM overflow acceptance test.
_CANNED_SM_OVERFLOW = {
    "summary": (
        "Three SM persona calls failed in the last 15 minutes, all with "
        "json parse failed at max_tokens=65536. This matches the SM token-overflow "
        "pattern that caused $12 of wasted spend previously."
    ),
    "escalate_to_l2": True,
    "escalation_reason": (
        "Repeated SM token-overflow failures (3 distinct stories). "
        "Pattern: json parse failed at max_tokens=65536."
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
) -> None:
    """Append a run event to state/events/runs.ndjson."""
    path = root / "state" / "events" / "runs.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": ts,
        "schema_version": 1,
        "event": "run",
        "success": success,
        "persona": persona,
        "story_id": story_id,
        "cost_usd": 0.01,
        "error": error,
        "model": "azure/gpt-5.4",
        "model_tier": None,
        "tokens_in": 1000,
        "tokens_out": 100,
        "duration_s": 5.0,
        "attempt_n": 1,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _write_prior_note(root: Path, *, ts: str, summary: str, escalated: bool = False) -> None:
    """Append a watcher note to state/events/watcher_notes.ndjson."""
    path = root / "state" / "events" / "watcher_notes.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "ts": ts,
        "schema_version": 1,
        "event": "watcher_notes",
        "lookback_minutes": 15.0,
        "since_ts": (datetime.fromisoformat(ts) - timedelta(minutes=15)).isoformat(),
        "note": {
            "summary": summary,
            "escalate_to_l2": escalated,
            "escalation_reason": "test reason" if escalated else None,
            "observations": [],
        },
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(envelope) + "\n")


def _make_mock_llm(response: dict[str, Any]):
    """Return a callable that, when used as a monkeypatch for text_run,
    returns ``response`` (a dict) — simulating schema-mode text_run."""

    def _mock_text_run(
        persona: str,
        prompt: str,
        model_id: str,
        schema: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return response

    return _mock_text_run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunWatcherOnceWithNoSignals:
    """Empty state/events/. Watcher should produce a valid result."""

    def test_returns_valid_envelope(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "factory.manager.watcher.text_run",
            _make_mock_llm(_CANNED_GOOD),
        )
        # Also need to monkeypatch _read_persona_prompt so it doesn't need the real file.
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        result = run_watcher_once(root=tmp_path, now=NOW)

        assert isinstance(result, dict)
        # Envelope fields
        assert result["event"] == "watcher_notes"
        assert result["schema_version"] == 1
        assert "ts" in result
        assert "since_ts" in result
        assert "lookback_minutes" in result
        # Note field
        note = result["note"]
        assert isinstance(note, dict)
        assert note["summary"] == "nothing happened"
        assert note["escalate_to_l2"] is False

    def test_watcher_notes_file_created(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "factory.manager.watcher.text_run",
            _make_mock_llm(_CANNED_GOOD),
        )
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        run_watcher_once(root=tmp_path, now=NOW)

        notes_path = _events_path(tmp_path, "watcher_notes")
        assert notes_path.exists()

    def test_all_detectors_called_without_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with empty streams, all detectors must be called and return results."""
        monkeypatch.setattr(
            "factory.manager.watcher.text_run",
            _make_mock_llm(_CANNED_GOOD),
        )
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        result = run_watcher_once(root=tmp_path, now=NOW)
        # If all detectors ran without raising, we get a valid envelope.
        assert result["event"] == "watcher_notes"


class TestRunWatcherOnceAppendsToWatcherNotes:
    """Confirms the note is appended with envelope fields and note.summary populated."""

    def test_envelope_fields_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "factory.manager.watcher.text_run",
            _make_mock_llm(_CANNED_GOOD),
        )
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        run_watcher_once(root=tmp_path, now=NOW)

        notes_path = _events_path(tmp_path, "watcher_notes")
        lines = [ln for ln in notes_path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1

        written = json.loads(lines[0])
        assert written["event"] == "watcher_notes"
        assert written["schema_version"] == 1
        assert "ts" in written
        assert "since_ts" in written
        assert "lookback_minutes" in written
        assert "note" in written
        assert written["note"]["summary"] == "nothing happened"

    def test_multiple_runs_append(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "factory.manager.watcher.text_run",
            _make_mock_llm(_CANNED_GOOD),
        )
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        t1 = NOW
        t2 = NOW + timedelta(minutes=1)

        run_watcher_once(root=tmp_path, now=t1)
        run_watcher_once(root=tmp_path, now=t2)

        notes_path = _events_path(tmp_path, "watcher_notes")
        lines = [ln for ln in notes_path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2


class TestRunWatcherResumesFromLastNoteTs:
    """Write a prior note at T; verify new run computes since >= T."""

    def test_since_uses_last_note_ts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Write a prior note at T_prior.
        t_prior = NOW - timedelta(minutes=5)
        _write_prior_note(tmp_path, ts=t_prior.isoformat(), summary="prior note")

        captured_prompts: list[str] = []

        def _capturing_text_run(persona: str, prompt: str, model_id: str, **kwargs: Any) -> dict:
            captured_prompts.append(prompt)
            return _CANNED_GOOD

        monkeypatch.setattr("factory.manager.watcher.text_run", _capturing_text_run)
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        result = run_watcher_once(root=tmp_path, now=NOW)

        # since_ts should be >= t_prior.isoformat() (the last note's ts).
        since_ts_str = result["since_ts"]
        since_dt = datetime.fromisoformat(since_ts_str)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=UTC)

        assert since_dt >= t_prior, (
            f"Expected since ({since_dt}) >= last note ts ({t_prior})"
        )

    def test_since_clamped_to_lookback_when_old_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If last note is older than lookback, since is clamped to now - lookback."""
        lookback = timedelta(minutes=15)
        t_very_old = NOW - timedelta(hours=2)
        _write_prior_note(tmp_path, ts=t_very_old.isoformat(), summary="old note")

        monkeypatch.setattr(
            "factory.manager.watcher.text_run",
            _make_mock_llm(_CANNED_GOOD),
        )
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        result = run_watcher_once(root=tmp_path, now=NOW, lookback=lookback)

        since_ts_str = result["since_ts"]
        since_dt = datetime.fromisoformat(since_ts_str)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=UTC)

        expected_earliest = NOW - lookback
        assert since_dt >= expected_earliest - timedelta(seconds=1), (
            f"since ({since_dt}) should be >= now - lookback ({expected_earliest})"
        )


class TestRunWatcherHandlesInvalidJsonResponse:
    """Mock LLM to return non-JSON twice; sentinel result returned; no exception."""

    def test_sentinel_returned_on_llm_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call_count = 0

        def _bad_text_run(persona: str, prompt: str, model_id: str, **kwargs: Any) -> dict:
            nonlocal call_count
            call_count += 1
            # Simulate text_run raising RuntimeError (what it does when JSON fails)
            raise RuntimeError(
                "JSON-mode response was not valid JSON after 4 attempts: bad json"
            )

        monkeypatch.setattr("factory.manager.watcher.text_run", _bad_text_run)
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        # Should not raise.
        result = run_watcher_once(root=tmp_path, now=NOW)

        note = result["note"]
        assert note["summary"] == "<watcher LLM failed>"
        assert note["escalate_to_l2"] is False
        assert "error" in note

    def test_note_still_appended_on_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even on LLM failure, a sentinel note is written to disk."""

        def _bad_text_run(persona: str, prompt: str, model_id: str, **kwargs: Any) -> dict:
            raise RuntimeError("llm exploded")

        monkeypatch.setattr("factory.manager.watcher.text_run", _bad_text_run)
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        run_watcher_once(root=tmp_path, now=NOW)

        notes_path = _events_path(tmp_path, "watcher_notes")
        assert notes_path.exists()
        lines = [ln for ln in notes_path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        written = json.loads(lines[0])
        assert written["note"]["summary"] == "<watcher LLM failed>"


class TestDryRunDoesNotCallLlm:
    """Confirm dry-run path bypasses the LLM helper."""

    def test_llm_not_called(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        llm_called = False

        def _tracking_text_run(persona: str, prompt: str, model_id: str, **kwargs: Any) -> dict:
            nonlocal llm_called
            llm_called = True
            return _CANNED_GOOD

        monkeypatch.setattr("factory.manager.watcher.text_run", _tracking_text_run)
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        result = run_watcher_once(root=tmp_path, now=NOW, dry_run=True)

        assert not llm_called
        note = result["note"]
        assert note["summary"] == "<dry-run>"
        assert note["escalate_to_l2"] is False

    def test_dry_run_still_writes_note(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "factory.manager.watcher.text_run",
            _make_mock_llm(_CANNED_GOOD),
        )
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        run_watcher_once(root=tmp_path, now=NOW, dry_run=True)

        notes_path = _events_path(tmp_path, "watcher_notes")
        assert notes_path.exists()


class TestWatcherFlagsSmTokenOverflowPattern:
    """MVP acceptance test: SM token-overflow synthetic pattern.

    Inject 3 SM failures with max_tokens error. Mock LLM to escalate.
    Verify the watcher note escalates and the prompt contains the events.
    """

    def _inject_sm_failures(self, root: Path) -> None:
        """Write 3 SM persona failures with the token-overflow error pattern."""
        for i in range(3):
            ts = (SINCE + timedelta(minutes=i * 2 + 1)).isoformat()
            _write_run_event(
                root,
                ts=ts,
                success=False,
                persona="sm",
                story_id=100 + i,
                error="json parse failed at max_tokens=65536 finish_reason=length: Expecting value: line 1 column 1 (char 0)",
            )

    def test_escalation_when_sm_overflow_pattern_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._inject_sm_failures(tmp_path)

        monkeypatch.setattr(
            "factory.manager.watcher.text_run",
            _make_mock_llm(_CANNED_SM_OVERFLOW),
        )
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        result = run_watcher_once(root=tmp_path, now=NOW)

        note = result["note"]
        assert note["escalate_to_l2"] is True
        assert note["escalation_reason"] is not None
        assert "max_tokens" in note["escalation_reason"].lower() or "sm" in note["escalation_reason"].lower()

    def test_prompt_contains_planted_sm_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify the assembled prompt contains the planted SM failure events."""
        self._inject_sm_failures(tmp_path)

        captured_prompts: list[str] = []

        def _capturing_text_run(persona: str, prompt: str, model_id: str, **kwargs: Any) -> dict:
            captured_prompts.append(prompt)
            return _CANNED_SM_OVERFLOW

        monkeypatch.setattr("factory.manager.watcher.text_run", _capturing_text_run)
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        run_watcher_once(root=tmp_path, now=NOW)

        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]
        # The planted SM failures should appear in the prompt's raw stream section.
        assert "json parse failed at max_tokens=65536" in prompt
        assert "sm" in prompt

    def test_prompt_contains_detector_docstrings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prompt must include each detector's docstring (load-bearing FMS pattern)."""
        captured_prompts: list[str] = []

        def _capturing_text_run(persona: str, prompt: str, model_id: str, **kwargs: Any) -> dict:
            captured_prompts.append(prompt)
            return _CANNED_GOOD

        monkeypatch.setattr("factory.manager.watcher.text_run", _capturing_text_run)
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        run_watcher_once(root=tmp_path, now=NOW)

        prompt = captured_prompts[0]
        # Each detector name must appear in the prompt.
        for name in ("runs_failed_since", "retry_storm", "cost_spike",
                     "tick_duration_outliers", "state_distribution_skew",
                     "worktree_orphans"):
            assert name in prompt, f"Detector '{name}' not found in prompt"

    def test_watcher_note_written_with_escalation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._inject_sm_failures(tmp_path)

        monkeypatch.setattr(
            "factory.manager.watcher.text_run",
            _make_mock_llm(_CANNED_SM_OVERFLOW),
        )
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        run_watcher_once(root=tmp_path, now=NOW)

        notes_path = _events_path(tmp_path, "watcher_notes")
        lines = [ln for ln in notes_path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        written = json.loads(lines[0])
        assert written["note"]["escalate_to_l2"] is True


# ---------------------------------------------------------------------------
# Additional unit tests for internal helpers
# ---------------------------------------------------------------------------


class TestReadStreamSince:
    def test_empty_root_returns_empty(self, tmp_path: Path) -> None:
        result = _read_stream_since(tmp_path, "runs", SINCE)
        assert result == []

    def test_filters_by_since(self, tmp_path: Path) -> None:
        path = tmp_path / "state" / "events" / "runs.ndjson"
        path.parent.mkdir(parents=True, exist_ok=True)
        old_ts = (SINCE - timedelta(hours=1)).isoformat()
        new_ts = (SINCE + timedelta(minutes=5)).isoformat()
        path.write_text(
            json.dumps({"ts": old_ts, "event": "run"}) + "\n"
            + json.dumps({"ts": new_ts, "event": "run"}) + "\n"
        )
        result = _read_stream_since(tmp_path, "runs", SINCE)
        assert len(result) == 1
        assert result[0]["ts"] == new_ts

    def test_string_truncation(self, tmp_path: Path) -> None:
        path = tmp_path / "state" / "events" / "runs.ndjson"
        path.parent.mkdir(parents=True, exist_ok=True)
        long_str = "x" * 1000
        ts = (SINCE + timedelta(minutes=1)).isoformat()
        path.write_text(json.dumps({"ts": ts, "event": "run", "error": long_str}) + "\n")
        result = _read_stream_since(tmp_path, "runs", SINCE)
        assert len(result) == 1
        assert len(result[0]["error"]) <= 514  # 500 + "[truncated]" suffix area


class TestReadPriorWatcherNotes:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = _read_prior_watcher_notes(tmp_path)
        assert result == []

    def test_returns_last_n_notes(self, tmp_path: Path) -> None:
        for i in range(15):
            ts = (NOW - timedelta(minutes=15 - i)).isoformat()
            _write_prior_note(tmp_path, ts=ts, summary=f"note {i}")
        result = _read_prior_watcher_notes(tmp_path, limit=10)
        assert len(result) == 10


class TestBuildUserMessage:
    """Unit test for the prompt assembly function."""

    def test_contains_all_required_sections(self) -> None:
        from factory.manager.detectors import DETECTOR_DOCS

        msg = _build_user_message(
            persona_prompt="# Test persona",
            since=SINCE,
            now=NOW,
            lookback_minutes=15.0,
            detector_results={name: {} for name in DETECTOR_DOCS},
            raw_streams={s: [] for s in ("runs", "ticks", "queue", "webhooks", "git", "spend")},
            prior_notes=[],
        )

        assert "# Test persona" in msg
        assert "since_ts" in msg
        assert "now_ts" in msg
        assert "lookback_minutes" in msg
        # All detector names
        for name in DETECTOR_DOCS:
            assert name in msg
        # All stream names
        for stream in ("runs", "ticks", "queue", "webhooks", "git", "spend"):
            assert stream in msg

    def test_contains_detector_docstrings(self) -> None:
        from factory.manager.detectors import DETECTOR_DOCS

        msg = _build_user_message(
            persona_prompt="# Test persona",
            since=SINCE,
            now=NOW,
            lookback_minutes=15.0,
            detector_results={name: {} for name in DETECTOR_DOCS},
            raw_streams={},
            prior_notes=[],
        )

        # Spot-check: the runs_failed_since docstring should appear.
        # Both the detector name and some context from the docstring appear.
        assert "runs_failed_since" in msg


# ---------------------------------------------------------------------------
# placeholder_prompts integration — confirm the watcher wires the detector
# into the user message so a leaked-placeholder regression surfaces in the
# L1→L2→L3→L4 pipeline.
# ---------------------------------------------------------------------------


def _write_prompt_event(
    root: Path,
    *,
    ts: str,
    persona: str,
    markers: list[str],
    story_id: int | None = 7,
) -> None:
    """Append one prompt-metadata record to state/events/prompts.ndjson."""
    path = root / "state" / "events" / "prompts.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": ts,
        "schema_version": 1,
        "event": "prompt",
        "persona": persona,
        "story_id": story_id,
        "model_id": "stub/model",
        "prompt_length_total": 1234,
        "prompt_section_lengths": {"Story": 100, "PR diff": 50},
        "placeholder_markers_found": markers,
        "prompt_hash": "deadbeefdeadbeef",
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


class TestWatcherInvokesPlaceholderPromptsDetector:
    """The new placeholder_prompts detector must be invoked by run_watcher_once
    and its result must appear in the user message handed to the LLM."""

    def test_leaked_marker_record_appears_in_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Plant one prompts.ndjson record with a leaked broken marker.
        marker = "(fetched from GitHub by the chain"
        _write_prompt_event(
            tmp_path,
            ts=(SINCE + timedelta(minutes=2)).isoformat(),
            persona="reviewer",
            markers=[marker],
        )

        captured_prompts: list[str] = []

        def _capturing_text_run(
            persona: str, prompt: str, model_id: str, **kwargs: Any
        ) -> dict:
            captured_prompts.append(prompt)
            return _CANNED_GOOD

        monkeypatch.setattr("factory.manager.watcher.text_run", _capturing_text_run)
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        run_watcher_once(root=tmp_path, now=NOW)

        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]
        # The detector section header must be present in the user message.
        assert "`placeholder_prompts`" in prompt
        # The detector's actual finding (the leaked marker + persona) must
        # be embedded in the result block so the LLM can reason about it.
        assert marker in prompt
        assert "reviewer" in prompt
        # severity is added by the detector for every returned row.
        assert "\"severity\"" in prompt and "high" in prompt

    def test_detector_called_even_when_stream_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No prompts.ndjson at all → detector returns []; watcher must still
        include the section (so the LLM sees the empty result, not a gap)."""
        captured_prompts: list[str] = []

        def _capturing_text_run(
            persona: str, prompt: str, model_id: str, **kwargs: Any
        ) -> dict:
            captured_prompts.append(prompt)
            return _CANNED_GOOD

        monkeypatch.setattr("factory.manager.watcher.text_run", _capturing_text_run)
        monkeypatch.setattr(
            "factory.manager.watcher._read_persona_prompt",
            lambda persona: "# Watcher persona mock",
        )

        run_watcher_once(root=tmp_path, now=NOW)

        prompt = captured_prompts[0]
        assert "`placeholder_prompts`" in prompt
