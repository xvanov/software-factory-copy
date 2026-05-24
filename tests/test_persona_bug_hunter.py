"""Tests for the Bug-Hunter persona — Phase 6.

Dry-run only; no LLM/GitHub. Verifies:

  * Fixture security finding → ``(security)``-typed direction.
  * Empty fixture produces no direction.
  * The persona prompt declares an Operating contract and requires JSON
    output (P7.0 cleanup — prompt-content assertions).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from factory.chain.scheduled_tasks import run_scheduled_persona

_FACTORY_ROOT = Path(__file__).resolve().parent.parent
_PERSONA_PATH = _FACTORY_ROOT / "factory" / "personas" / "bug_hunter.md"


def test_persona_bug_hunter_prompt_has_operating_contract() -> None:
    """P7.0 cleanup: every persona prompt must declare its Operating contract."""
    body = _PERSONA_PATH.read_text(encoding="utf-8")
    assert "## Operating contract" in body, "bug_hunter.md missing 'Operating contract' section"


def test_persona_bug_hunter_prompt_requires_json_output() -> None:
    """P7.0 cleanup: Bug-Hunter emits structured JSON; the prompt must say so."""
    body = _PERSONA_PATH.read_text(encoding="utf-8")
    assert "JSON" in body, "bug_hunter.md missing JSON output requirement"
    assert "```json" in body, "bug_hunter.md missing fenced JSON output schema"


def _write_root(tmp_path: Path) -> Path:
    apps = tmp_path / "apps" / "sacrifice"
    apps.mkdir(parents=True)
    (apps / "config.yaml").write_text(
        yaml.safe_dump({"name": "sacrifice", "repo": "o/r"}), encoding="utf-8"
    )
    (tmp_path / "state").mkdir(parents=True)
    return tmp_path


def test_bug_hunter_security_finding_files_security_direction(tmp_path: Path) -> None:
    """Fixture has one ``severity=high`` semgrep hit with type=security."""
    root = _write_root(tmp_path)
    out = run_scheduled_persona("bug_hunter", "sacrifice", root, dry_run=True)
    assert out.findings_count == 1
    assert len(out.directions_filed) == 1
    direction_id = out.directions_filed[0]
    matches = list((root / "apps" / "sacrifice" / "directions").glob(f"{direction_id}-*"))
    assert len(matches) == 1
    md = (matches[0] / "direction.md").read_text(encoding="utf-8")
    assert "type: security" in md


def test_bug_hunter_empty_fixture_files_no_direction(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    out = run_scheduled_persona(
        "bug_hunter",
        "sacrifice",
        root,
        dry_run=True,
        fixture_output={"findings": [], "runs_completed": [], "duration_s": 0.0},
    )
    assert out.findings_count == 0
    assert out.directions_filed == []
