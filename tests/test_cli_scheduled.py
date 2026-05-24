"""CLI tests for the Phase-6 force-fire commands + ``factory schedules``.

Uses Typer's CliRunner. Each force-fire is invoked with ``--dry-run`` so
no LLM/network is required. The tests run against the production
factory root (so the real ``factory_settings.yaml`` is exercised) but
isolate side effects by pointing apps at ``tmp_path`` apps subdirectory
where possible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from factory.cli import app as cli_app


def _runner_invoke(args: list[str], **kwargs: Any) -> Any:
    return CliRunner().invoke(cli_app, args, **kwargs)


def test_factory_schedules_lists_four_default_schedules() -> None:
    # rich's Table truncates long names; assert on a prefix that
    # uniquely identifies each schedule.
    res = _runner_invoke(["schedules"])
    assert res.exit_code == 0, res.stdout + res.stderr
    out = res.stdout
    for prefix in ("ralph", "bug_hunt", "ux_audit", "security_"):
        assert prefix in out, f"missing {prefix} in:\n{out}"


def _patch_root_to_tmp(monkeypatch: Any, tmp_path: Path) -> Path:
    """Point ``factory.cli._FACTORY_ROOT`` and dotenv reads at ``tmp_path``.

    Lets the CLI tests file directions under a tmpdir instead of the real
    apps/sacrifice/directions/ tree.
    """
    apps = tmp_path / "apps" / "sacrifice"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / "config.yaml").write_text(
        yaml.safe_dump({"name": "sacrifice", "repo": "o/r"}), encoding="utf-8"
    )
    (tmp_path / "factory_settings.yaml").write_text(
        "modes:\n  default: normal\n  available: [normal, fix-only, paused]\n",
        encoding="utf-8",
    )
    (tmp_path / "state").mkdir(exist_ok=True)
    monkeypatch.setattr("factory.cli._FACTORY_ROOT", tmp_path)
    return tmp_path


def test_ralph_now_dry_run_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    _patch_root_to_tmp(monkeypatch, tmp_path)
    res = _runner_invoke(["ralph-now", "--app", "sacrifice", "--dry-run"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "dry_run" in res.stdout
    # One direction was filed.
    dirs = list((tmp_path / "apps" / "sacrifice" / "directions").iterdir())
    assert len(dirs) == 1


def test_bug_hunt_now_dry_run_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    _patch_root_to_tmp(monkeypatch, tmp_path)
    res = _runner_invoke(["bug-hunt-now", "--app", "sacrifice", "--dry-run"])
    assert res.exit_code == 0, res.stdout + res.stderr
    dirs = list((tmp_path / "apps" / "sacrifice" / "directions").iterdir())
    assert len(dirs) == 1


def test_ux_audit_now_dry_run_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    _patch_root_to_tmp(monkeypatch, tmp_path)
    res = _runner_invoke(["ux-audit-now", "--app", "sacrifice", "--dry-run"])
    assert res.exit_code == 0, res.stdout + res.stderr
    dirs = list((tmp_path / "apps" / "sacrifice" / "directions").iterdir())
    assert len(dirs) == 1


def test_security_now_dry_run_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    _patch_root_to_tmp(monkeypatch, tmp_path)
    res = _runner_invoke(["security-now", "--app", "sacrifice", "--dry-run"])
    assert res.exit_code == 0, res.stdout + res.stderr
    dirs = list((tmp_path / "apps" / "sacrifice" / "directions").iterdir())
    assert len(dirs) == 1
