"""Tests for ``factory apps`` command and its underlying ``list_apps`` function."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner


def _write_app_config(
    apps_root: Path, name: str, repo: str, self_tick: bool, deploy_enabled: bool
) -> Path:
    """Write a minimal ``apps/<name>/config.yaml`` and return the app dir."""
    app_dir = apps_root / name
    app_dir.mkdir(parents=True)
    cfg = {
        "name": name,
        "repo": repo,
        "self_tick_enabled": self_tick,
        "deploy": {"enabled": deploy_enabled},
    }
    (app_dir / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return app_dir


def test_list_apps_returns_every_app_with_config(tmp_path: Path) -> None:
    """AC1.1 + AC4.1: list_apps discovers both apps, one row each."""
    from factory.app_config import list_apps

    apps_root = tmp_path / "apps"
    _write_app_config(apps_root, "alpha", "owner/alpha", self_tick=True, deploy_enabled=True)
    _write_app_config(apps_root, "beta", "owner/beta", self_tick=False, deploy_enabled=False)

    rows = list_apps(tmp_path)
    assert len(rows) == 2
    names = {r["name"] for r in rows}
    assert names == {"alpha", "beta"}


def test_list_apps_includes_required_fields(tmp_path: Path) -> None:
    """AC2.1 + AC2.2: each row carries name, repo, self_tick_enabled, deploy_enabled."""
    from factory.app_config import list_apps

    apps_root = tmp_path / "apps"
    _write_app_config(apps_root, "alpha", "owner/alpha", self_tick=True, deploy_enabled=True)
    _write_app_config(apps_root, "beta", "owner/beta", self_tick=False, deploy_enabled=False)

    rows = {r["name"]: r for r in list_apps(tmp_path)}

    alpha = rows["alpha"]
    assert alpha["name"] == "alpha"
    assert alpha["repo"] == "owner/alpha"
    assert alpha["self_tick_enabled"] is True
    assert alpha["deploy_enabled"] is True

    beta = rows["beta"]
    assert beta["name"] == "beta"
    assert beta["repo"] == "owner/beta"
    assert beta["self_tick_enabled"] is False
    assert beta["deploy_enabled"] is False


def test_list_apps_read_only(tmp_path: Path) -> None:
    """AC3.1: list_apps does not mutate the config or filesystem."""
    from factory.app_config import list_apps

    apps_root = tmp_path / "apps"
    cfg_path = apps_root / "alpha" / "config.yaml"
    _write_app_config(apps_root, "alpha", "owner/alpha", self_tick=True, deploy_enabled=True)

    original_yaml = cfg_path.read_text(encoding="utf-8")

    list_apps(tmp_path)
    list_apps(tmp_path)  # Call twice to ensure idempotency

    assert cfg_path.read_text(encoding="utf-8") == original_yaml
    # No state files or side effects created.
    children = list(tmp_path.iterdir())
    assert {p.name for p in children} == {"apps"}


def test_list_apps_no_configs_returns_empty(tmp_path: Path) -> None:
    """When no apps/*/config.yaml exist, list_apps returns an empty list."""
    from factory.app_config import list_apps

    (tmp_path / "apps").mkdir()
    rows = list_apps(tmp_path)
    assert rows == []


def test_cli_apps_output_contains_required_fields(tmp_path: Path) -> None:
    """AC4.2 + AC4.3: CLI output shows both apps with self_tick_enabled and deploy.enabled."""
    import importlib

    import factory.cli as cli_mod
    from factory.settings.loader import reload_settings

    apps_root = tmp_path / "apps"
    _write_app_config(apps_root, "alpha", "owner/alpha", self_tick=True, deploy_enabled=True)
    _write_app_config(apps_root, "beta", "owner/beta", self_tick=False, deploy_enabled=False)

    # Minimal factory_settings so reload_settings doesn't look for a real file.
    (tmp_path / "factory_settings.yaml").write_text(
        yaml.safe_dump({"caps": {}, "modes": {"default": "normal", "available": ["normal"]}}),
        encoding="utf-8",
    )
    (tmp_path / "state").mkdir()

    reload_settings(tmp_path)
    importlib.reload(cli_mod)
    cli_mod._FACTORY_ROOT = tmp_path  # type: ignore[attr-defined]

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["apps"])

    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "beta" in result.output
    assert "owner/alpha" in result.output
    assert "owner/beta" in result.output
    assert "self_tick_enabled" in result.output
    assert "deploy.enabled" in result.output
    # True / False values appear in the rendered table.
    assert "True" in result.output
    assert "False" in result.output


def test_cli_apps_exit_zero_when_apps_found(tmp_path: Path) -> None:
    """AC3.2: exits 0 when at least one app is found."""
    import importlib

    import factory.cli as cli_mod
    from factory.settings.loader import reload_settings

    apps_root = tmp_path / "apps"
    _write_app_config(apps_root, "alpha", "owner/alpha", self_tick=False, deploy_enabled=False)

    (tmp_path / "factory_settings.yaml").write_text(
        yaml.safe_dump({"caps": {}, "modes": {"default": "normal", "available": ["normal"]}}),
        encoding="utf-8",
    )
    (tmp_path / "state").mkdir()

    reload_settings(tmp_path)
    importlib.reload(cli_mod)
    cli_mod._FACTORY_ROOT = tmp_path  # type: ignore[attr-defined]

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["apps"])
    assert result.exit_code == 0
