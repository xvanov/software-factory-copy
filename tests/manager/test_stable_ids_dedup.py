"""Tests for stable content-hash IDs + unified FMS dedup (Tier 2 WS2.2).

Covers the three previously-inconsistent dedup keys, now unified on stable,
content-derived ids that build on the WS0.1 concern signature:

* concern_id == the WS0.1 signature, resolvable on legacy docs;
* L3 (_is_concern_processed) dedups on concern_id, not the LLM title:
    - same content (even retitled by formatting) → deduped;
    - a title collision with genuinely-different content → NOT suppressed;
    - a legacy proposal without concern_id still dedups via concern_title.
* proposal_id is stable for the same source concern regardless of ts-slug path;
* L4 (_is_already_processed) dedups a re-emitted proposal on proposal_id even
  under a fresh path, and still honours legacy path-only history.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from factory.manager.apply import (
    _append_history,
    _is_already_processed,
)
from factory.manager.diagnostician import (
    _is_concern_processed,
    _proposal_id,
    _proposals_dir,
    run_diagnostician_once,
)
from factory.manager.summarizer import _concern_signature, concern_id_for

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _concern(
    *,
    title: str = "sm-token-overflow-loop",
    proposed_area: str = "persona_settings",
    urgency: str = "warn",
    evidence_kind: str = "run",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "title": title,
        "description": "A repeated failure pattern.",
        "evidence": [{"kind": evidence_kind, "id": 1, "excerpt": "boom"}],
        "proposed_area": proposed_area,
        "urgency": urgency,
        "escalate_to_l3": True,
        "escalation_reason": "repeated",
    }


def _write_concern(root: Path, concern: dict[str, Any], slug: str = "c") -> Path:
    concerns_dir = root / "state" / "concerns"
    concerns_dir.mkdir(parents=True, exist_ok=True)
    path = concerns_dir / f"20260719T115500-{slug}.json"
    path.write_text(json.dumps(concern, indent=2), encoding="utf-8")
    return path


def _write_proposal_file(
    root: Path,
    *,
    concern_title: str,
    concern_id: str | None,
    proposal_id: str | None = None,
    slug: str = "p",
    ts: str = "20260719T120000",
) -> Path:
    proposals_dir = _proposals_dir(root)
    proposals_dir.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {
        "schema_version": 1,
        "concern_title": concern_title,
        "diagnosis": "done",
        "proposal": {"kind": "prompt_edit", "target": "x", "suggested_patch": ""},
        "target_class": "prompt_edit",
        "escalate_to_human": False,
    }
    if concern_id is not None:
        doc["concern_id"] = concern_id
    if proposal_id is not None:
        doc["proposal_id"] = proposal_id
    path = proposals_dir / f"{ts}-{slug}.json"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _mock_persona_prompt(persona: str) -> str:
    return f"# {persona} mock persona"


def _patch_llm(monkeypatch: pytest.MonkeyPatch, tracker: dict[str, bool]) -> None:
    def _text_run(persona, prompt, model_id, schema=None, **kwargs):  # noqa: ANN001
        tracker["called"] = True
        return {
            "concern_title": "sm-token-overflow-loop",
            "diagnosis": "d",
            "proposal": {
                "kind": "prompt_edit",
                "target": "factory/personas/sm.md",
                "rationale": "r",
                "suggested_patch": "",
                "verification": "",
                "confidence": "high",
            },
            "target_class": "prompt_edit",
            "escalate_to_human": False,
            "escalation_reason": None,
        }

    monkeypatch.setattr("factory.manager.diagnostician.text_run", _text_run)
    monkeypatch.setattr(
        "factory.manager.diagnostician._read_persona_prompt", _mock_persona_prompt
    )
    import factory.model_router as mr

    monkeypatch.setattr(mr, "route", lambda persona: "anthropic/claude-opus-4-7")
    monkeypatch.setattr(mr, "max_output_tokens_for", lambda model_id: 32768)


# ---------------------------------------------------------------------------
# concern_id builds on the WS0.1 signature
# ---------------------------------------------------------------------------


def test_concern_id_equals_ws01_signature() -> None:
    c = _concern()
    assert concern_id_for(c) == _concern_signature(c)


def test_concern_id_prefers_stamped_field() -> None:
    c = _concern()
    c["concern_id"] = "explicit-id"
    assert concern_id_for(c) == "explicit-id"


def test_concern_id_falls_back_to_signature_field() -> None:
    """A legacy doc with only ``signature`` resolves to the same id."""
    c = _concern()
    sig = _concern_signature(c)
    legacy = {k: v for k, v in c.items()}
    legacy["signature"] = sig  # no concern_id
    assert concern_id_for(legacy) == sig


def test_concern_id_stable_across_formatting_retitle() -> None:
    """Case/whitespace-only retitles normalise to the same id (the retitle bug)."""
    a = _concern(title="SM Token Overflow Loop")
    b = _concern(title="sm token   overflow loop")
    assert concern_id_for(a) == concern_id_for(b)


def test_concern_id_differs_on_real_content_change() -> None:
    a = _concern(proposed_area="persona_settings")
    b = _concern(proposed_area="dispatch_code")
    assert concern_id_for(a) != concern_id_for(b)


# ---------------------------------------------------------------------------
# L3 dedup on concern_id
# ---------------------------------------------------------------------------


def test_l3_dedups_same_concern(tmp_path: Path) -> None:
    c = _concern()
    _write_proposal_file(
        tmp_path, concern_title=c["title"], concern_id=concern_id_for(c)
    )
    assert _is_concern_processed(tmp_path, c) is True


def test_l3_dedups_retitled_same_content(tmp_path: Path) -> None:
    """The bug this fixes: a formatting-retitled concern with the same content
    is recognised via concern_id even though the proposal's concern_title differs.
    """
    original = _concern(title="SM Token Overflow Loop")
    retitled = _concern(title="sm token   overflow loop")
    # Proposal was written for the original title, carrying the stable id.
    _write_proposal_file(
        tmp_path,
        concern_title=original["title"],
        concern_id=concern_id_for(original),
    )
    # Different title string, same content id → still deduped.
    assert retitled["title"] != original["title"]
    assert _is_concern_processed(tmp_path, retitled) is True


def test_l3_processes_title_collision_different_content(tmp_path: Path) -> None:
    """Two concerns share a title but differ in content → NOT suppressed."""
    first = _concern(title="shared-title", proposed_area="persona_settings")
    second = _concern(title="shared-title", proposed_area="dispatch_code")
    _write_proposal_file(
        tmp_path, concern_title=first["title"], concern_id=concern_id_for(first)
    )
    assert concern_id_for(first) != concern_id_for(second)
    # The genuinely-different second concern must still be processed.
    assert _is_concern_processed(tmp_path, second) is False


def test_l3_legacy_proposal_title_fallback(tmp_path: Path) -> None:
    """A legacy proposal without concern_id still dedups via concern_title."""
    c = _concern()
    _write_proposal_file(tmp_path, concern_title=c["title"], concern_id=None)
    assert _is_concern_processed(tmp_path, c) is True


def test_l3_new_concern_never_suppressed_by_unrelated_proposal(tmp_path: Path) -> None:
    other = _concern(title="unrelated", proposed_area="observability")
    _write_proposal_file(
        tmp_path, concern_title=other["title"], concern_id=concern_id_for(other)
    )
    fresh = _concern(title="brand-new", proposed_area="dispatch_code")
    assert _is_concern_processed(tmp_path, fresh) is False


def test_run_diagnostician_dedups_refired_concern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a concern already having a matching proposal is not re-diagnosed."""
    c = _concern()
    _write_concern(tmp_path, c)
    _write_proposal_file(
        tmp_path, concern_title=c["title"], concern_id=concern_id_for(c)
    )
    tracker = {"called": False}
    _patch_llm(monkeypatch, tracker)
    result = run_diagnostician_once(root=tmp_path, now=NOW)
    assert result is None
    assert tracker["called"] is False


# ---------------------------------------------------------------------------
# proposal_id stability + emission
# ---------------------------------------------------------------------------


def test_proposal_id_stable_for_same_concern() -> None:
    c = _concern()
    cid = concern_id_for(c)
    area = c["proposed_area"]
    assert _proposal_id(cid, area) == _proposal_id(cid, area)


def test_proposal_id_differs_for_different_concern() -> None:
    a = _concern(proposed_area="persona_settings")
    b = _concern(proposed_area="dispatch_code")
    assert _proposal_id(concern_id_for(a), a["proposed_area"]) != _proposal_id(
        concern_id_for(b), b["proposed_area"]
    )


def test_diagnostician_stamps_stable_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _concern()
    concern_path = _write_concern(tmp_path, c)
    tracker = {"called": False}
    _patch_llm(monkeypatch, tracker)
    result = run_diagnostician_once(
        root=tmp_path, concern_path=concern_path, now=NOW
    )
    assert result is not None
    cid = concern_id_for(c)
    assert result["concern_id"] == cid
    assert result["proposal_id"] == _proposal_id(cid, c["proposed_area"])
    # And the written file carries them too.
    written = json.loads(Path(result["proposal_path"]).read_text(encoding="utf-8"))
    assert written["concern_id"] == cid
    assert written["proposal_id"] == result["proposal_id"]


# ---------------------------------------------------------------------------
# L4 dedup on proposal_id
# ---------------------------------------------------------------------------


def test_l4_dedups_reemitted_proposal_by_id_new_path(tmp_path: Path) -> None:
    """A re-emitted proposal under a fresh ts-slug path is deduped by proposal_id."""
    pid = "stable-proposal-id"
    _append_history(
        tmp_path,
        {
            "proposal_path": str(tmp_path / "state/manager_proposals/OLD-path.json"),
            "proposal_id": pid,
            "concern_id": "cid",
            "status": "opened_pr",
        },
    )
    new_path = tmp_path / "state/manager_proposals/20260719T130000-NEW-path.json"
    proposal = {"proposal_id": pid, "concern_id": "cid"}
    assert _is_already_processed(tmp_path, new_path, proposal) is True


def test_l4_processes_new_proposal_id(tmp_path: Path) -> None:
    _append_history(
        tmp_path,
        {
            "proposal_path": str(tmp_path / "state/manager_proposals/OLD.json"),
            "proposal_id": "id-A",
        },
    )
    new_path = tmp_path / "state/manager_proposals/NEW.json"
    proposal = {"proposal_id": "id-B"}
    assert _is_already_processed(tmp_path, new_path, proposal) is False


def test_l4_legacy_history_path_fallback(tmp_path: Path) -> None:
    """History entry lacking proposal_id still dedups by exact path."""
    p = tmp_path / "state/manager_proposals/legacy.json"
    _append_history(tmp_path, {"proposal_path": str(p), "status": "opened_pr"})
    # Even with no proposal dict, the path match holds.
    assert _is_already_processed(tmp_path, p) is True
    # And a proposal that carries an id still matches on the legacy path.
    assert _is_already_processed(tmp_path, p, {"proposal_id": "x"}) is True


def test_l4_empty_history_processes(tmp_path: Path) -> None:
    p = tmp_path / "state/manager_proposals/fresh.json"
    assert _is_already_processed(tmp_path, p, {"proposal_id": "id"}) is False
