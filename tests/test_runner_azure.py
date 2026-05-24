"""Azure AI Foundry provider plumbing (Phase 8).

These tests exercise the env remap, the LiteLLM-detection monkey-patch, and
the runner's API-key resolution for ``azure_ai/...`` model ids. No network
is touched — Stage C smoke tests cover the live endpoint manually.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory import model_router, providers
from factory.providers import azure_foundry
from factory.runner import LLMConfig, _resolve_api_key


@pytest.fixture(autouse=True)
def _reset_azure_bootstrap() -> None:
    """Allow ``ensure_bootstrapped`` to run again between tests."""
    azure_foundry.reset_for_tests()
    yield
    azure_foundry.reset_for_tests()


def test_resolve_api_key_for_azure_ai_reads_dedicated_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``azure_ai/<model>`` → reads ``AZURE_AI_API_KEY``."""
    monkeypatch.delenv("AZURE_FOUNDRY_API_KEY", raising=False)
    monkeypatch.setenv("AZURE_AI_API_KEY", "azure-key-A")
    cfg = LLMConfig(model="azure_ai/gpt-4.1")
    assert _resolve_api_key(cfg) == "azure-key-A"


def test_resolve_api_key_for_azure_ai_falls_back_to_foundry_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ``AZURE_FOUNDRY_API_KEY`` set → resolver still finds the key
    (the bootstrap remap copies it into ``AZURE_AI_API_KEY``)."""
    monkeypatch.delenv("AZURE_AI_API_KEY", raising=False)
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "foundry-key-B")
    cfg = LLMConfig(model="azure_ai/gpt-4.1")
    assert _resolve_api_key(cfg) == "foundry-key-B"


def test_ensure_bootstrapped_remaps_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The remap copies all three AZURE_FOUNDRY_* names into AZURE_AI_API_*."""
    monkeypatch.delenv("AZURE_AI_API_BASE", raising=False)
    monkeypatch.delenv("AZURE_AI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_AI_API_VERSION", raising=False)
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://example.test/models")
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "k123")
    monkeypatch.setenv("AZURE_FOUNDRY_API_VERSION", "2024-05-01-preview")

    azure_foundry.ensure_bootstrapped()

    import os

    assert os.environ["AZURE_AI_API_BASE"] == "https://example.test/models"
    assert os.environ["AZURE_AI_API_KEY"] == "k123"
    assert os.environ["AZURE_AI_API_VERSION"] == "2024-05-01-preview"


def test_ensure_bootstrapped_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling ``ensure_bootstrapped`` twice does not raise nor re-mutate env
    that the user explicitly set between calls."""
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "first")
    monkeypatch.setenv("AZURE_AI_API_KEY", "kept-by-user")
    azure_foundry.ensure_bootstrapped()
    azure_foundry.ensure_bootstrapped()
    import os

    # The user's explicit AZURE_AI_API_KEY value must be preserved.
    assert os.environ["AZURE_AI_API_KEY"] == "kept-by-user"


def test_ensure_bootstrapped_patches_litellm_azure_ai_detection() -> None:
    """The monkey-patch forces ``_is_azure_openai_model`` to return False
    so LiteLLM's OpenAI-compatible azure_ai path is used for every model
    (including ``gpt-4.1``, which would otherwise downgrade to the
    Azure-OpenAI deployment URL that does not exist on a Foundry endpoint).
    """
    azure_foundry.ensure_bootstrapped()
    from litellm.llms.azure_ai.chat.transformation import AzureAIStudioConfig

    cfg = AzureAIStudioConfig()
    assert cfg._is_azure_openai_model("gpt-4.1", None) is False
    assert cfg._is_azure_openai_model("gpt-4o-mini", None) is False


def test_routes_yaml_default_provider_is_azure() -> None:
    """The shipped ``routes.yaml`` must declare azure as default for Phase 8."""
    import os

    # Clear any test-only override (e.g. ``test_model_router.py``'s fixture).
    os.environ.pop("FACTORY_PROVIDER", None)
    assert model_router.active_provider() == "azure"


def test_route_returns_azure_model_under_default_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``default_provider: azure`` every persona resolves to an
    ``azure_ai/...`` model id."""
    monkeypatch.delenv("FACTORY_PROVIDER", raising=False)
    for persona in (
        "pm",
        "analyst",
        "architect",
        "sm",
        "test_designer",
        "reviewer",
        "tech_writer",
        "onboarder",
        "security",
    ):
        model_id = model_router.route(persona)
        assert model_id.startswith("azure_ai/"), f"{persona} routed to {model_id!r}"


def test_factory_provider_env_override_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FACTORY_PROVIDER=direct`` overrides the YAML's ``default_provider``."""
    monkeypatch.setenv("FACTORY_PROVIDER", "direct")
    assert model_router.route("pm") == "deepseek/deepseek-chat"
    monkeypatch.setenv("FACTORY_PROVIDER", "azure")
    assert model_router.route("pm") == "azure_ai/gpt-4.1"


def test_route_uses_azure_fallback_when_persona_missing(tmp_path: Path) -> None:
    """A persona absent from ``azure_routes`` falls back to ``azure_fallback``."""
    custom = tmp_path / "r.yaml"
    custom.write_text(
        "default_provider: azure\n"
        "azure_routes:\n"
        "  pm: azure_ai/gpt-4.1\n"
        "defaults:\n"
        "  fallback: deepseek/deepseek-chat\n"
        "  azure_fallback: azure_ai/gpt-4.1\n",
        encoding="utf-8",
    )
    assert model_router.route("nonexistent", routes_path=custom) == "azure_ai/gpt-4.1"


# Silence the providers-import-only lint by reaching the module symbol.
_ = providers
