"""Model router resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.model_router import all_known_personas, route


def test_static_persona_returns_string() -> None:
    assert route("pm") == "deepseek/deepseek-chat"
    assert route("reviewer") == "anthropic/claude-opus-4-7"


def test_dev_difficulty_branches() -> None:
    assert route("dev", "standard") == "deepseek/deepseek-coder"
    assert route("dev", "hard") == "anthropic/claude-sonnet-4-6"


def test_unknown_persona_falls_back() -> None:
    # Falls back to defaults.fallback per routes.yaml
    assert route("nonexistent_persona") == "deepseek/deepseek-chat"


def test_dev_default_difficulty_is_standard() -> None:
    assert route("dev") == "deepseek/deepseek-coder"


def test_all_known_personas_lists_expected() -> None:
    personas = all_known_personas()
    assert "dev" in personas
    assert "pm" in personas
    assert "reviewer" in personas


def test_fallback_used_when_difficulty_missing(tmp_path: Path) -> None:
    custom = tmp_path / "r.yaml"
    custom.write_text(
        "routes:\n"
        "  dev:\n"
        "    standard: deepseek/deepseek-coder\n"
        "defaults:\n"
        "  fallback: deepseek/deepseek-chat\n",
        encoding="utf-8",
    )
    # hard not defined → falls back to standard
    assert route("dev", "hard", routes_path=custom) == "deepseek/deepseek-coder"


def test_keyerror_when_no_fallback(tmp_path: Path) -> None:
    custom = tmp_path / "r.yaml"
    custom.write_text("routes:\n  pm: x/y\n", encoding="utf-8")
    with pytest.raises(KeyError):
        route("nonexistent", routes_path=custom)
