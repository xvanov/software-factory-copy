"""Model router resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.model_router import active_provider, all_known_personas, route


@pytest.fixture(autouse=True)
def _force_direct_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The historical model_router tests assert the direct-provider routes.

    The factory default flipped to ``azure`` in Phase 8; pin these tests to
    ``direct`` so the assertions about DeepSeek/OpenRouter model ids stay
    meaningful. Azure-specific behavior lives in ``test_runner_azure.py``.

    Provider keys are pinned as PRESENT so the router's key-aware degradation
    (see ``test_key_aware_degradation``) doesn't rewrite the assertions based
    on the developer's shell environment.
    """
    monkeypatch.setenv("FACTORY_PROVIDER", "direct")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_API_KEY", "test-key")


def test_static_persona_returns_string() -> None:
    assert route("pm") == "deepseek/deepseek-chat"
    assert route("reviewer") == "openrouter/z-ai/glm-5.2"


def test_dev_difficulty_branches() -> None:
    assert route("dev", "standard") == "deepseek/deepseek-coder"
    assert route("dev", "hard") == "openrouter/moonshotai/kimi-k2.7-code"


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


def test_active_provider_reflects_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_PROVIDER", "azure")
    assert active_provider() == "azure"
    monkeypatch.setenv("FACTORY_PROVIDER", "direct")
    assert active_provider() == "direct"


class TestKeyAwareDegradation:
    """Missing provider keys degrade a route to the block fallback (and only
    when the fallback's own key IS available) — see route() docstring."""

    def test_degrades_to_fallback_when_route_key_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTORY_PROVIDER", "azure")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("AZURE_API_KEY", "test-key")
        assert route("reviewer") == "azure/gpt-5.4"
        assert route("dev", "hard") == "azure/gpt-5.4"

    def test_intended_route_activates_once_key_lands(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTORY_PROVIDER", "azure")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        monkeypatch.setenv("AZURE_API_KEY", "test-key")
        assert route("reviewer") == "openrouter/z-ai/glm-5.2"
        assert route("manager_watcher") == "openrouter/deepseek/deepseek-v4-flash"
        assert route("dev", "hard") == "openrouter/moonshotai/kimi-k2.7-code"

    def test_keeps_route_when_fallback_key_also_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # direct block: fallback is deepseek/deepseek-chat — with BOTH keys
        # missing the original route is returned (the runner reports the
        # missing-key error with the intended model, not a masked fallback).
        monkeypatch.setenv("FACTORY_PROVIDER", "direct")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        assert route("reviewer") == "openrouter/z-ai/glm-5.2"

    def test_azure_foundry_key_counts_for_azure_models(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FACTORY_PROVIDER", "azure")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("AZURE_API_KEY", raising=False)
        monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "test-key")
        assert route("reviewer") == "azure/gpt-5.4"
