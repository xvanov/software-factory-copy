"""Tests for the UX-Auditor persona — Phase 6.

Dry-run only; no LLM/GitHub/browser. Verifies:

  * Fixture friction finding → ``(ux)``-typed direction.
  * Empty fixture produces no direction.
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


def test_ux_auditor_friction_finding_files_ux_direction(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    out = run_scheduled_persona("ux_auditor", "sacrifice", root, dry_run=True)
    assert out.findings_count == 1
    assert len(out.directions_filed) == 1
    direction_id = out.directions_filed[0]
    matches = list((root / "apps" / "sacrifice" / "directions").glob(f"{direction_id}-*"))
    assert len(matches) == 1
    md = (matches[0] / "direction.md").read_text(encoding="utf-8")
    assert "type: ux" in md
    # Finding must cite a flow + step per the persona contract.
    assert "pledge" in md.lower() or "click" in md.lower()


def test_ux_auditor_empty_fixture_files_no_direction(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    out = run_scheduled_persona(
        "ux_auditor",
        "sacrifice",
        root,
        dry_run=True,
        fixture_output={"findings": [], "duration_s": 0.0},
    )
    assert out.findings_count == 0
    assert out.directions_filed == []
