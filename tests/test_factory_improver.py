"""Factory-improver persona — chain handler + CLI + idempotent GH posting.

What we verify:
  * ``aggregate_factory_needs_redesign_events`` walks ``state/logs/*.log``,
    pulls just the redesign events in the time window, ignores malformed
    lines, returns oldest-first.
  * ``run_factory_improver`` (dry-run) writes the proposal JSON to
    ``state/improvements/<ts>.json`` and reports the event count.
  * ``post_to_pinned_issue`` is idempotent: when an issue with the
    ``factory-improvements`` label exists, it COMMENTS on it instead of
    opening a fresh one; when none exists, it OPENS one with the label.
  * The persona prompt lives at the renamed path and the route exists.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from factory.chain.factory_improver import (
    FactoryImproverResult,
    aggregate_factory_needs_redesign_events,
    post_to_pinned_issue,
    run_factory_improver,
)


# ---------------------------------------------------------------------------
# Persona file + route
# ---------------------------------------------------------------------------


def test_persona_file_exists_and_has_required_sections() -> None:
    """The active persona prompt at ``factory/personas/factory_improver.md``
    must carry the canonical sections the chain expects."""
    root = Path(__file__).resolve().parent.parent
    md = root / "factory" / "personas" / "factory_improver.md"
    assert md.exists(), f"missing persona file: {md}"
    text = md.read_text(encoding="utf-8")
    assert "factory_improver" in text
    assert "Operating contract" in text
    assert "Output schema" in text
    assert "improvements" in text  # JSON output schema
    assert "prompt_edit" in text  # kind enum
    assert "new_state" in text


def test_stub_file_removed() -> None:
    """The old ``_factory_improver.md`` stub is gone — the persona is active."""
    root = Path(__file__).resolve().parent.parent
    stub = root / "factory" / "personas" / "_factory_improver.md"
    assert not stub.exists(), "old stub _factory_improver.md should be removed"


def test_route_entry_exists() -> None:
    """``factory/routes.yaml`` carries a route for ``factory_improver``."""
    import yaml

    root = Path(__file__).resolve().parent.parent
    routes = yaml.safe_load((root / "factory" / "routes.yaml").read_text(encoding="utf-8"))
    direct = routes.get("routes") or {}
    azure = routes.get("azure_routes") or {}
    assert "factory_improver" in direct
    assert "factory_improver" in azure


def test_cron_schedule_entry_exists() -> None:
    """``factory_settings.yaml`` schedules the improver."""
    import yaml

    root = Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((root / "factory_settings.yaml").read_text(encoding="utf-8"))
    names = [s["name"] for s in (cfg.get("schedules") or [])]
    assert "factory_improver" in names


# ---------------------------------------------------------------------------
# Event aggregation
# ---------------------------------------------------------------------------


def _write_log(
    logs_dir: Path, name: str, events: list[dict[str, Any]]
) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    with (logs_dir / name).open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_aggregate_filters_by_event_type(tmp_path: Path) -> None:
    """Only ``factory_needs_redesign`` events are returned."""
    logs = tmp_path / "state" / "logs"
    now = datetime.now(UTC)
    _write_log(
        logs,
        "0001-x.log",
        [
            {"ts": now.isoformat(), "event": "handler_start"},
            {
                "ts": now.isoformat(),
                "event": "factory_needs_redesign",
                "retries_used": 3,
                "suggestions": ["import error"],
            },
            {"ts": now.isoformat(), "event": "dev_retry"},
        ],
    )
    events = aggregate_factory_needs_redesign_events(
        software_factory_root=tmp_path, window_hours=24
    )
    assert len(events) == 1
    assert events[0]["event"] == "factory_needs_redesign"


def test_aggregate_filters_by_time_window(tmp_path: Path) -> None:
    """Events older than ``window_hours`` are excluded."""
    logs = tmp_path / "state" / "logs"
    now = datetime.now(UTC)
    old_ts = (now - timedelta(hours=48)).isoformat()
    recent_ts = (now - timedelta(hours=2)).isoformat()
    _write_log(
        logs,
        "0001-x.log",
        [
            {"ts": old_ts, "event": "factory_needs_redesign", "tag": "old"},
            {"ts": recent_ts, "event": "factory_needs_redesign", "tag": "recent"},
        ],
    )
    events = aggregate_factory_needs_redesign_events(
        software_factory_root=tmp_path, window_hours=24, now=now
    )
    assert len(events) == 1
    assert events[0]["tag"] == "recent"


def test_aggregate_tolerates_malformed_lines(tmp_path: Path) -> None:
    """Malformed JSON lines and missing-ts records are skipped silently."""
    logs = tmp_path / "state" / "logs"
    logs.mkdir(parents=True)
    now = datetime.now(UTC)
    (logs / "0001-x.log").write_text(
        "this is not json\n"
        + json.dumps({"event": "factory_needs_redesign"})  # missing ts
        + "\n"
        + json.dumps({"ts": now.isoformat(), "event": "factory_needs_redesign", "ok": True})
        + "\n",
        encoding="utf-8",
    )
    events = aggregate_factory_needs_redesign_events(
        software_factory_root=tmp_path, window_hours=24
    )
    assert len(events) == 1
    assert events[0]["ok"] is True


def test_aggregate_returns_empty_when_no_logs(tmp_path: Path) -> None:
    """A factory root with no ``state/logs/`` returns []."""
    assert (
        aggregate_factory_needs_redesign_events(software_factory_root=tmp_path) == []
    )


def test_aggregate_sorts_oldest_first(tmp_path: Path) -> None:
    """Events from multiple log files are returned oldest-first."""
    logs = tmp_path / "state" / "logs"
    now = datetime.now(UTC)
    t1 = (now - timedelta(hours=3)).isoformat()
    t2 = (now - timedelta(hours=2)).isoformat()
    t3 = (now - timedelta(hours=1)).isoformat()
    _write_log(
        logs,
        "0001-a.log",
        [{"ts": t1, "event": "factory_needs_redesign", "tag": "t1"}],
    )
    _write_log(
        logs,
        "0002-b.log",
        [
            {"ts": t3, "event": "factory_needs_redesign", "tag": "t3"},
            {"ts": t2, "event": "factory_needs_redesign", "tag": "t2"},
        ],
    )
    events = aggregate_factory_needs_redesign_events(
        software_factory_root=tmp_path, window_hours=24
    )
    tags = [e["tag"] for e in events]
    assert tags == ["t1", "t2", "t3"]


# ---------------------------------------------------------------------------
# Idempotent pinned-issue posting
# ---------------------------------------------------------------------------


@dataclass
class _StubCompleted:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def test_pinned_issue_comments_when_issue_exists() -> None:
    """When ``gh issue list`` returns an open issue with the label,
    we post a comment rather than opening a new issue."""
    calls: list[list[str]] = []

    def _runner(args: list[str], **kwargs: Any) -> _StubCompleted:
        calls.append(args)
        if args[:3] == ["gh", "issue", "list"]:
            return _StubCompleted(
                returncode=0, stdout=json.dumps([{"number": 42, "title": "x"}])
            )
        if args[:3] == ["gh", "issue", "comment"]:
            return _StubCompleted(returncode=0, stdout="commented")
        return _StubCompleted(returncode=1, stderr="unexpected")

    number, err = post_to_pinned_issue(
        repo="owner/repo", body="test body", gh_runner=_runner
    )
    assert err is None
    assert number == 42
    # First call is list; second is comment on issue 42.
    assert calls[0][:3] == ["gh", "issue", "list"]
    assert calls[1][:3] == ["gh", "issue", "comment"]
    assert "42" in calls[1]
    # We did NOT call create.
    assert not any(c[:3] == ["gh", "issue", "create"] for c in calls)


def test_pinned_issue_creates_when_none_exists() -> None:
    """When ``gh issue list`` returns [], a new issue with the
    ``factory-improvements`` label is opened."""
    calls: list[list[str]] = []

    def _runner(args: list[str], **kwargs: Any) -> _StubCompleted:
        calls.append(args)
        if args[:3] == ["gh", "issue", "list"]:
            return _StubCompleted(returncode=0, stdout="[]")
        if args[:3] == ["gh", "issue", "create"]:
            return _StubCompleted(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/77\n",
            )
        return _StubCompleted(returncode=1)

    number, err = post_to_pinned_issue(
        repo="owner/repo", body="test", gh_runner=_runner
    )
    assert err is None
    assert number == 77
    # We DID call create with the label.
    create_call = next(c for c in calls if c[:3] == ["gh", "issue", "create"])
    assert "--label" in create_call
    label_idx = create_call.index("--label")
    assert create_call[label_idx + 1] == "factory-improvements"


def test_pinned_issue_returns_error_on_list_failure() -> None:
    """If ``gh issue list`` fails, the error is reported (not raised)."""

    def _runner(args: list[str], **kwargs: Any) -> _StubCompleted:
        return _StubCompleted(returncode=1, stderr="auth failed")

    number, err = post_to_pinned_issue(
        repo="owner/repo", body="x", gh_runner=_runner
    )
    assert number is None
    assert err is not None and "gh_list_failed" in err


# ---------------------------------------------------------------------------
# run_factory_improver (dry-run end-to-end)
# ---------------------------------------------------------------------------


def test_run_factory_improver_dry_writes_output(tmp_path: Path) -> None:
    """Dry-run path: persists a proposal JSON, no LLM, no GH call."""
    # Seed an event so the fixture produces at least one improvement.
    logs = tmp_path / "state" / "logs"
    now = datetime.now(UTC)
    _write_log(
        logs,
        "0001-x.log",
        [
            {
                "ts": now.isoformat(),
                "event": "factory_needs_redesign",
                "retries_used": 3,
                "suggestions": ["import error in conftest"],
            }
        ],
    )

    result = run_factory_improver(
        app=None,
        software_factory_root=tmp_path,
        window_hours=24,
        dry_run=True,
    )

    assert isinstance(result, FactoryImproverResult)
    assert result.succeeded
    assert result.events_processed == 1
    assert result.improvements_count >= 1
    assert result.output_path is not None
    assert result.output_path.exists()
    persisted = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert "improvements" in persisted
    assert "summary" in persisted
    assert persisted["events_processed"] == 1


def test_run_factory_improver_dry_with_no_events(tmp_path: Path) -> None:
    """Dry-run with no events produces zero improvements, healthy summary."""
    (tmp_path / "state").mkdir()
    result = run_factory_improver(
        app=None,
        software_factory_root=tmp_path,
        dry_run=True,
    )
    assert result.succeeded
    assert result.events_processed == 0
    persisted = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert persisted["improvements"] == []


def test_run_factory_improver_dry_with_fixture_output(tmp_path: Path) -> None:
    """``fixture_output`` lets tests inject a specific proposal shape."""
    (tmp_path / "state").mkdir()
    fixture = {
        "improvements": [
            {
                "kind": "prompt_edit",
                "target": "factory/personas/dev.md",
                "rationale": "test rationale",
                "suggested_patch": "Append: emit SELF_SUMMARY.",
                "evidence": "log:foo",
                "confidence": "high",
            }
        ],
        "summary": "fixture summary",
        "events_processed": 0,
    }
    result = run_factory_improver(
        app=None,
        software_factory_root=tmp_path,
        dry_run=True,
        fixture_output=fixture,
    )
    assert result.succeeded
    assert result.improvements_count == 1
    persisted = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert persisted["summary"] == "fixture summary"
    assert persisted["improvements"][0]["kind"] == "prompt_edit"


# ---------------------------------------------------------------------------
# L2 apply-pass wiring
# ---------------------------------------------------------------------------


def test_run_factory_improver_skips_apply_in_dry_run(tmp_path: Path) -> None:
    """Dry-run never triggers the L2 apply pass (no subprocess, no
    branches, no PRs)."""
    (tmp_path / "state").mkdir()
    result = run_factory_improver(
        app=None,
        software_factory_root=tmp_path,
        dry_run=True,
        fixture_output={
            "improvements": [
                {
                    "kind": "prompt_edit",
                    "target": "factory/personas/dev.md",
                    "rationale": "x",
                    "suggested_patch": "free-text",
                }
            ],
            "summary": "s",
            "events_processed": 0,
        },
        apply_pass=True,
    )
    assert result.succeeded
    assert result.apply_summary is None


def test_run_factory_improver_skips_apply_when_no_improvements(
    tmp_path: Path,
) -> None:
    """An empty proposals list skips the apply pass — nothing to do."""
    (tmp_path / "state").mkdir()
    result = run_factory_improver(
        app=None,
        software_factory_root=tmp_path,
        dry_run=True,
        fixture_output={
            "improvements": [],
            "summary": "healthy",
            "events_processed": 0,
        },
    )
    assert result.succeeded
    assert result.apply_summary is None
