"""Tests for ``factory.chain.ralph.ralph_tick`` (Phase 6.A).

Dry-run with the built-in fixture findings creates two new directions
(one ``bug``, one ``docs``) under ``apps/<app>/directions/``.

Rate-limit trip: ``ralph_runs_per_day: 0`` in factory_settings.yaml
forces ``can_dispatch("ralph", ...)`` to refuse before any direction is
created.

NO subprocess.run, NO LLM calls, NO GitHub interactions in dry-run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from factory.chain.ralph import RalphSummary, ralph_tick


def _write_root(tmp_path: Path, ralph_cap: int | None = None) -> Path:
    """Stage a factory root with sacrifice's app config + settings."""
    apps = tmp_path / "apps" / "sacrifice"
    apps.mkdir(parents=True)
    cfg: dict[str, Any] = {
        "name": "sacrifice",
        "repo": "o/r",
        "default_branch": "main",
        "deploy": {"enabled": False},
    }
    (apps / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    settings: dict[str, Any] = {
        "modes": {"default": "normal", "available": ["normal", "paused"]},
    }
    if ralph_cap is not None:
        settings["rate_limits"] = {"ralph_runs_per_day": ralph_cap}
    (tmp_path / "factory_settings.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    (tmp_path / "state").mkdir()
    from factory.settings.loader import reload_settings

    reload_settings(tmp_path)
    return tmp_path


def test_ralph_tick_dry_run_creates_two_directions(tmp_path: Path) -> None:
    """Built-in fixture has 1 spec_drift + 1 docs_drift → 2 directions filed."""
    root = _write_root(tmp_path)
    summary = ralph_tick("sacrifice", root, dry_run=True)
    assert isinstance(summary, RalphSummary)
    assert summary.allowed
    assert summary.rejected_reason is None
    assert summary.findings_count == 2
    assert len(summary.directions_created) == 2

    # Walk apps/sacrifice/directions/ and assert both directions are on disk.
    dir_root = root / "apps" / "sacrifice" / "directions"
    children = sorted(p.name for p in dir_root.iterdir())
    assert len(children) == 2
    # Each direction has a direction.md.
    for name in children:
        assert (dir_root / name / "direction.md").exists()
        assert (dir_root / name / "state.yaml").exists()


def test_ralph_tick_uses_suggested_type_tag(tmp_path: Path) -> None:
    """Bug-typed findings → ``type: bug``; docs-typed → ``type: docs``."""
    root = _write_root(tmp_path)
    ralph_tick("sacrifice", root, dry_run=True)
    types_seen: set[str] = set()
    import frontmatter  # type: ignore[import-untyped]

    for d in (root / "apps" / "sacrifice" / "directions").iterdir():
        post = frontmatter.load(str(d / "direction.md"))
        types_seen.add(str(post.get("type") or ""))
    assert "bug" in types_seen
    assert "docs" in types_seen


def test_ralph_tick_rate_limit_zero_refuses(tmp_path: Path) -> None:
    """Setting ralph_runs_per_day:0 causes ralph_tick to refuse.

    Acceptance criterion (Phase 6 G #7): rejected_reason must be exactly
    ``ralph_rate_limit_exceeded`` and NO direction is created.
    """
    root = _write_root(tmp_path, ralph_cap=0)
    summary = ralph_tick("sacrifice", root, dry_run=True)
    assert not summary.allowed
    assert summary.rejected_reason == "ralph_rate_limit_exceeded"
    # No findings processed, no directions filed.
    assert summary.findings_count == 0
    assert summary.directions_created == []
    assert not (root / "apps" / "sacrifice" / "directions").exists() or not list(
        (root / "apps" / "sacrifice" / "directions").iterdir()
    )


def test_ralph_tick_honors_fixture_findings(tmp_path: Path) -> None:
    """Passing ``fixture_findings`` overrides the built-in fixture."""
    root = _write_root(tmp_path)
    fixture = {
        "findings": [
            {
                "kind": "missing_test",
                "path": "backend/foo.py",
                "claim": "/foo returns 200",
                "evidence": "no test covers /foo",
                "suggested_direction_type": "bug",
                "suggested_direction_title": "Test /foo happy path",
            }
        ],
        "summary": "1 missing-test finding.",
    }
    summary = ralph_tick("sacrifice", root, dry_run=True, fixture_findings=fixture)
    assert summary.findings_count == 1
    assert len(summary.directions_created) == 1
    # Title should be reflected in the direction's frontmatter.
    import frontmatter  # type: ignore[import-untyped]

    d = next((root / "apps" / "sacrifice" / "directions").iterdir())
    post = frontmatter.load(str(d / "direction.md"))
    assert post.get("title") == "Test /foo happy path"
    assert post.get("type") == "bug"


def test_ralph_tick_caps_findings_at_twenty(tmp_path: Path) -> None:
    """A runaway persona returning 100 findings is capped at 20."""
    root = _write_root(tmp_path)
    many = {
        "findings": [
            {
                "kind": "docs_drift",
                "path": f"file_{i}.py",
                "claim": f"claim {i}",
                "evidence": f"ev {i}",
                "suggested_direction_type": "docs",
                "suggested_direction_title": f"Docs drift #{i}",
            }
            for i in range(100)
        ],
        "summary": "100 findings.",
    }
    summary = ralph_tick("sacrifice", root, dry_run=True, fixture_findings=many)
    assert summary.findings_count == 20
    assert len(summary.directions_created) == 20
