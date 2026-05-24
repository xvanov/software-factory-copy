"""Tests for the bug_hunter scheduled persona (Phase 6 B).

Dry-run with the built-in fixture creates a security-tagged direction.
Rate-limit trip refuses the run with the canonical ``rejected_reason``.

NO subprocess.run for scanners, NO LLM calls in dry-run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from factory.chain.scheduled_tasks import run_scheduled_persona


def _write_root(tmp_path: Path, cap: int | None = None) -> Path:
    apps = tmp_path / "apps" / "sacrifice"
    apps.mkdir(parents=True)
    (apps / "config.yaml").write_text(
        yaml.safe_dump({"name": "sacrifice", "repo": "o/r"}), encoding="utf-8"
    )
    settings: dict[str, Any] = {
        "modes": {"default": "normal", "available": ["normal", "paused"]},
    }
    if cap is not None:
        settings["rate_limits"] = {"bug_hunter_runs_per_day": cap}
    (tmp_path / "factory_settings.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    (tmp_path / "state").mkdir()
    from factory.settings.loader import reload_settings

    reload_settings(tmp_path)
    return tmp_path


def test_bug_hunter_dry_run_files_security_direction(tmp_path: Path) -> None:
    """The built-in fixture has 1 high-severity semgrep finding tagged ``security``."""
    root = _write_root(tmp_path)
    out = run_scheduled_persona("bug_hunter", "sacrifice", root, dry_run=True)
    assert out.status == "dry_run"
    assert out.findings_count == 1
    assert len(out.directions_filed) == 1
    matches = list(
        (root / "apps" / "sacrifice" / "directions").glob(f"{out.directions_filed[0]}-*")
    )
    assert len(matches) == 1
    md = (matches[0] / "direction.md").read_text(encoding="utf-8")
    assert "type: security" in md
    assert "shell=True" in md


def test_bug_hunter_rate_limit_zero_refuses(tmp_path: Path) -> None:
    """``bug_hunter_runs_per_day: 0`` short-circuits the run."""
    root = _write_root(tmp_path, cap=0)
    out = run_scheduled_persona("bug_hunter", "sacrifice", root, dry_run=True)
    assert out.status == "rejected"
    assert out.error == "bug_hunter_rate_limit_exceeded"
    assert out.findings_count == 0
    assert out.directions_filed == []
    # No directions filed.
    dir_root = root / "apps" / "sacrifice" / "directions"
    assert not dir_root.exists() or not list(dir_root.iterdir())
