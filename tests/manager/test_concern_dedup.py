"""Tests for L2 concern dedup + cooldown + stable signature (WS0.1).

A persistent condition (e.g. a healthy drain) previously re-fired the SAME
concern every summarizer cycle. These tests assert:

* the signature is stable across cycles for a materially-identical concern and
  ignores volatile facets (timestamps, run IDs, counts);
* a same-signature concern within the cooldown window is SUPPRESSED (no file,
  no event);
* a genuinely different concern (different signature) is still emitted;
* after the cooldown elapses, the same concern emits again.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from factory.manager.summarizer import (
    _CONCERN_DEDUP_COOLDOWN,
    _concern_signature,
    _concerns_dir,
    _events_path,
    run_summarizer_once,
)

NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


def _write_watcher_note(root: Path, *, ts: str, summary: str) -> None:
    path = root / "state" / "events" / "watcher_notes.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "ts": ts,
        "schema_version": 1,
        "event": "watcher_notes",
        "note": {
            "summary": summary,
            "escalate_to_l2": True,
            "escalation_reason": "test",
            "observations": [],
        },
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(envelope) + "\n")


def _concern(title: str = "healthy-drain-loop", urgency: str = "warn") -> dict[str, Any]:
    return {
        "title": title,
        "description": "desc",
        "evidence": [
            {"kind": "watcher_note", "ts": "2026-05-26T11:57:00+00:00", "summary_excerpt": "x"},
            {"kind": "tick", "ts": "2026-05-26T11:58:00+00:00", "excerpt": "y"},
        ],
        "proposed_area": "observability",
        "urgency": urgency,
        "escalate_to_l3": True,
        "escalation_reason": "test",
    }


def _patch(monkeypatch: pytest.MonkeyPatch, response_holder: dict) -> None:
    def _mock_text_run(persona, prompt, model_id, schema=None, **kwargs):
        return response_holder["value"]

    monkeypatch.setattr("factory.manager.summarizer.text_run", _mock_text_run)
    monkeypatch.setattr(
        "factory.manager.summarizer._read_persona_prompt",
        lambda persona: "# L2 persona mock",
    )


# ---------------------------------------------------------------------------
# Signature stability
# ---------------------------------------------------------------------------


def test_signature_stable_ignores_volatile_facets() -> None:
    a = _concern()
    b = _concern()
    # Change volatile facets only: timestamps and evidence ids/excerpts.
    b["evidence"][0]["ts"] = "2026-05-26T23:59:00+00:00"
    b["evidence"][0]["summary_excerpt"] = "totally different text"
    b["description"] = "a completely different description"
    assert _concern_signature(a) == _concern_signature(b)


def test_signature_differs_on_material_change() -> None:
    a = _concern()
    b = _concern(title="a-different-concern-entirely")
    assert _concern_signature(a) != _concern_signature(b)
    # Different urgency also changes it.
    c = _concern(urgency="halt")
    assert _concern_signature(a) != _concern_signature(c)


# ---------------------------------------------------------------------------
# Dedup + cooldown through run_summarizer_once
# ---------------------------------------------------------------------------


def test_same_signature_within_cooldown_suppressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = {"value": _concern()}
    _patch(monkeypatch, holder)

    # First emission.
    _write_watcher_note(tmp_path, ts=NOW.isoformat(), summary="drain 1")
    first = run_summarizer_once(root=tmp_path, now=NOW)
    assert first is not None
    assert not first.get("suppressed")
    assert "concern_path" in first
    assert Path(first["concern_path"]).exists()

    # Second emission within cooldown, fresh note, same concern -> suppressed.
    t2 = NOW + timedelta(minutes=5)
    _write_watcher_note(tmp_path, ts=t2.isoformat(), summary="drain 2")
    second = run_summarizer_once(root=tmp_path, now=t2)
    assert second is not None
    assert second.get("suppressed") is True
    assert second.get("reason") == "duplicate_within_cooldown"

    # Only ONE concern file and ONE concern_emitted event were written.
    files = list(_concerns_dir(tmp_path).glob("*.json"))
    assert len(files) == 1
    events = _events_path(tmp_path, "concerns").read_text().strip().splitlines()
    assert len(events) == 1


def test_different_signature_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = {"value": _concern()}
    _patch(monkeypatch, holder)

    _write_watcher_note(tmp_path, ts=NOW.isoformat(), summary="drain 1")
    first = run_summarizer_once(root=tmp_path, now=NOW)
    assert first is not None and not first.get("suppressed")

    # Different concern (different title) within the cooldown -> still emitted.
    holder["value"] = _concern(title="brand-new-anomaly")
    t2 = NOW + timedelta(minutes=5)
    _write_watcher_note(tmp_path, ts=t2.isoformat(), summary="new anomaly")
    second = run_summarizer_once(root=tmp_path, now=t2)
    assert second is not None
    assert not second.get("suppressed")
    assert Path(second["concern_path"]).exists()

    assert len(list(_concerns_dir(tmp_path).glob("*.json"))) == 2


def test_same_signature_after_cooldown_emitted_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = {"value": _concern()}
    _patch(monkeypatch, holder)

    _write_watcher_note(tmp_path, ts=NOW.isoformat(), summary="drain 1")
    first = run_summarizer_once(root=tmp_path, now=NOW)
    assert first is not None and not first.get("suppressed")

    # Past the cooldown, same concern -> emits again.
    t2 = NOW + _CONCERN_DEDUP_COOLDOWN + timedelta(minutes=1)
    _write_watcher_note(tmp_path, ts=t2.isoformat(), summary="drain later")
    second = run_summarizer_once(root=tmp_path, now=t2)
    assert second is not None
    assert not second.get("suppressed")
    assert Path(second["concern_path"]).exists()

    assert len(list(_concerns_dir(tmp_path).glob("*.json"))) == 2
