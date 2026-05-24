"""Tests for the Ralph (continuous-improvement) persona — Phase 6.

Dry-run only; no LLM/GitHub. Verifies:

  * Fixture drift produces a ``(bug)``-typed direction on disk.
  * Empty fixture produces no direction.
  * The ``scheduled_runs`` row is recorded with the right counts.
  * The persona prompt declares an Operating contract and requires JSON
    output (P7.0 cleanup — prompt-content assertions).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from factory.chain.scheduled_tasks import (
    ScheduledRunRecord,
    run_scheduled_persona,
)

_FACTORY_ROOT = Path(__file__).resolve().parent.parent
_PERSONA_PATH = _FACTORY_ROOT / "factory" / "personas" / "ralph.md"


def test_persona_ralph_prompt_has_operating_contract() -> None:
    """P7.0 cleanup: every persona prompt must declare its Operating contract."""
    body = _PERSONA_PATH.read_text(encoding="utf-8")
    assert "## Operating contract" in body, "ralph.md missing 'Operating contract' section"


def test_persona_ralph_prompt_requires_json_output() -> None:
    """P7.0 cleanup: Ralph emits structured JSON; the prompt must say so."""
    body = _PERSONA_PATH.read_text(encoding="utf-8")
    assert "JSON" in body, "ralph.md missing JSON output requirement"
    assert "```json" in body, "ralph.md missing fenced JSON output schema"


def _write_root(tmp_path: Path) -> Path:
    apps = tmp_path / "apps" / "sacrifice"
    apps.mkdir(parents=True)
    (apps / "config.yaml").write_text(
        yaml.safe_dump({"name": "sacrifice", "repo": "o/r"}), encoding="utf-8"
    )
    (tmp_path / "state").mkdir(parents=True)
    return tmp_path


def test_ralph_dry_run_files_direction_for_fixture_drift(tmp_path: Path) -> None:
    """The default fixture has one spec-drift drift; one direction must land."""
    root = _write_root(tmp_path)
    out = run_scheduled_persona("ralph", "sacrifice", root, dry_run=True)
    assert out.status == "dry_run"
    assert out.findings_count == 1
    assert len(out.directions_filed) == 1
    # Direction landed under the dry-run scratch tree (Phase 7 keeps
    # apps/<app>/directions/ untouched on dry-run paths).
    direction_id = out.directions_filed[0]
    scratch = root / "state" / "dry_run_scratch" / "apps" / "sacrifice" / "directions"
    matches = list(scratch.glob(f"{direction_id}-*"))
    assert len(matches) == 1
    # The canonical tree stays clean.
    canonical = root / "apps" / "sacrifice" / "directions"
    assert not canonical.exists() or not list(canonical.glob(f"{direction_id}-*"))
    direction_md = (matches[0] / "direction.md").read_text(encoding="utf-8")
    assert "fix /healthz" in direction_md
    assert "type: bug" in direction_md


def test_ralph_empty_fixture_files_no_direction(tmp_path: Path) -> None:
    """A fixture with no drifts produces zero directions but still records the run."""
    root = _write_root(tmp_path)
    out = run_scheduled_persona(
        "ralph",
        "sacrifice",
        root,
        dry_run=True,
        fixture_output={"drifts": [], "runs_completed": [], "duration_s": 0.0},
    )
    assert out.findings_count == 0
    assert out.directions_filed == []


def test_ralph_records_scheduled_run_row(tmp_path: Path) -> None:
    """Every dispatch — success, skip, error — must persist a ScheduledRunRecord."""
    from sqlmodel import Session, create_engine, select

    root = _write_root(tmp_path)
    run_scheduled_persona("ralph", "sacrifice", root, dry_run=True)
    db = root / "state" / "factory.db"
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        rows = list(session.exec(select(ScheduledRunRecord)).all())
    assert len(rows) == 1
    assert rows[0].persona == "ralph"
    assert rows[0].app == "sacrifice"
    assert rows[0].status == "dry_run"
    assert rows[0].findings_count == 1
    assert rows[0].dry_run is True
