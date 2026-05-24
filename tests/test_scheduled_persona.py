"""Tests for ``run_scheduled_persona`` end-to-end (without an LLM).

Covers:

  * Unknown persona → errored without filing directions.
  * A finding without ``suggested_direction`` is silently skipped.
  * A multi-finding fixture produces multiple directions in one run.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from factory.chain.scheduled_tasks import run_scheduled_persona


def _write_root(tmp_path: Path) -> Path:
    apps = tmp_path / "apps" / "sacrifice"
    apps.mkdir(parents=True)
    (apps / "config.yaml").write_text(
        yaml.safe_dump({"name": "sacrifice", "repo": "o/r"}), encoding="utf-8"
    )
    (tmp_path / "state").mkdir(parents=True)
    return tmp_path


def test_unknown_persona_errors_without_filing_directions(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    out = run_scheduled_persona("not_a_persona", "sacrifice", root, dry_run=True)
    assert out.status == "errored"
    assert out.findings_count == 0
    assert out.directions_filed == []
    assert "unknown_scheduled_persona" in (out.error or "")


def test_finding_without_suggested_direction_is_skipped(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    fx = {
        "findings": [
            {"tool": "semgrep", "rule_id": "x", "severity": "low"},  # no suggested_direction
        ]
    }
    out = run_scheduled_persona("bug_hunter", "sacrifice", root, dry_run=True, fixture_output=fx)
    assert out.findings_count == 1
    assert out.directions_filed == []


def test_multi_finding_fixture_files_multiple_directions(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    fx = {
        "findings": [
            {
                "suggested_direction": {
                    "title": "fix A",
                    "type": "bug",
                    "why": "A is broken",
                    "acceptance": ["A works"],
                }
            },
            {
                "suggested_direction": {
                    "title": "fix B",
                    "type": "refactor",
                    "why": "B is gnarly",
                    "acceptance": ["B is clean"],
                }
            },
        ]
    }
    out = run_scheduled_persona("bug_hunter", "sacrifice", root, dry_run=True, fixture_output=fx)
    assert out.findings_count == 2
    assert len(out.directions_filed) == 2
    # IDs must be unique.
    assert len(set(out.directions_filed)) == 2
