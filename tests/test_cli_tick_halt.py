"""Tests for tick_cmd halt-check (Phase 8).

Verifies that when the factory is halted, ``factory tick`` exits cleanly
without invoking the factory_improver or scheduled personas.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner


def _make_root(tmp_path: Path) -> Path:
    """Create a minimal factory root with sacrifice app config."""
    root = tmp_path / "root"
    root.mkdir()
    app_dir = root / "apps" / "sacrifice"
    app_dir.mkdir(parents=True)
    (app_dir / "config.yaml").write_text(
        "name: sacrifice\nrepo: https://github.com/test/sacrifice\ndefault_branch: main\n",
        encoding="utf-8",
    )
    (root / "state").mkdir(parents=True, exist_ok=True)
    return root


def _set_halt(root: Path) -> None:
    """Write a halt state file."""
    import json
    from datetime import UTC, datetime

    state = {
        "schema_version": 1,
        "mode": "halted",
        "set_at": datetime.now(UTC).isoformat(),
        "set_by": "manager_diagnostician",
        "concern_title": "test-halt",
        "proposal_path": None,
        "reason": "test halt for tick_cmd test",
    }
    halt_path = root / "state" / "factory_mode.json"
    halt_path.parent.mkdir(parents=True, exist_ok=True)
    halt_path.write_text(json.dumps(state), encoding="utf-8")


def _get_cli(root: Path):  # type: ignore[return]
    """Return a CliRunner + cli module with _FACTORY_ROOT patched to root."""
    import importlib

    import factory.cli as cli_mod

    importlib.reload(cli_mod)
    cli_mod._FACTORY_ROOT = root  # type: ignore[attr-defined]
    return CliRunner(), cli_mod


def test_tick_cmd_skips_improver_when_halted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When halted, tick_cmd prints halt state and exits 0 without calling improver."""
    root = _make_root(tmp_path)
    _set_halt(root)

    improver_called = False

    def _loud_improver(*args: Any, **kwargs: Any) -> Any:
        nonlocal improver_called
        improver_called = True
        raise AssertionError("factory_improver must NOT be called when halted")

    monkeypatch.setattr(
        "factory.chain.factory_improver.should_fire_improver",
        lambda *a, **kw: (False, "halted"),
    )
    monkeypatch.setattr(
        "factory.chain.factory_improver.run_factory_improver",
        _loud_improver,
    )

    runner, cli_mod = _get_cli(root)
    # The tick_cmd checks halt BEFORE calling anything, so it should exit 0.
    result = runner.invoke(cli_mod.app, ["tick", "--app", "sacrifice", "--dry-run"])

    # Should exit cleanly.
    assert result.exit_code == 0, (
        f"tick should exit 0 when halted, got {result.exit_code}. "
        f"Output:\n{result.stdout}"
    )
    # Improver must not have been invoked.
    assert not improver_called, "factory_improver must not be called when factory is halted"
    # Halt notice should appear in output.
    assert "HALTED" in result.stdout or "halted" in result.stdout.lower(), (
        f"Expected halt notice in output. Got:\n{result.stdout}"
    )


def test_tick_cmd_proceeds_when_not_halted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When not halted, tick_cmd proceeds normally (no early exit)."""
    root = _make_root(tmp_path)
    # No halt file.

    # Mock out the heavy parts so the test is fast.
    monkeypatch.setattr(
        "factory.chain.factory_improver.should_fire_improver",
        lambda *a, **kw: (False, "dry-run"),
    )

    runner, cli_mod = _get_cli(root)
    result = runner.invoke(cli_mod.app, ["tick", "--app", "sacrifice", "--dry-run"])

    # Should reach the tick logic (exit 0 since no stories in flight).
    assert result.exit_code == 0, (
        f"tick should succeed when not halted. Output:\n{result.stdout}"
    )
    # Should NOT contain a halt message.
    assert "HALTED" not in result.stdout, (
        f"Unexpected halt message in output:\n{result.stdout}"
    )
