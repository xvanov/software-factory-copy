"""Azure provider plumbing.

Covers BOTH Azure surfaces:

* ``azure_ai/...`` — Foundry path. Env vars: AZURE_FOUNDRY_* / AZURE_AI_API_*.
* ``azure/...``    — Azure OpenAI / Cognitive Services. Env vars:
                     AZURE_ENDPOINT / AZURE_API_BASE / AZURE_API_KEY /
                     AZURE_API_VERSION.

No network is touched — Stage C smoke tests cover the live endpoint manually.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory import model_router, providers
from factory.providers import azure_foundry
from factory.runner import LLMConfig, _provider_env_key, _resolve_api_key


@pytest.fixture(autouse=True)
def _reset_azure_bootstrap() -> None:
    """Allow ``ensure_bootstrapped`` to run again between tests."""
    azure_foundry.reset_for_tests()
    yield
    azure_foundry.reset_for_tests()


# --------------------------------------------------------------------------- #
# Foundry surface (azure_ai/...)
# --------------------------------------------------------------------------- #


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


def test_ensure_bootstrapped_remaps_foundry_env(monkeypatch: pytest.MonkeyPatch) -> None:
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


# --------------------------------------------------------------------------- #
# Azure-OpenAI / Cognitive Services surface (azure/...)
# --------------------------------------------------------------------------- #


def test_provider_env_key_distinguishes_two_azure_prefixes() -> None:
    """``azure_ai/`` and ``azure/`` resolve to DIFFERENT env-var names.

    The two surfaces share neither URL shape nor key scope; conflating their
    keys silently sends Foundry traffic to an Azure-OpenAI key (and vice
    versa) — the test guards that boundary explicitly.
    """
    assert _provider_env_key("azure_ai/gpt-4.1") == "AZURE_AI_API_KEY"
    assert _provider_env_key("azure/gpt-5.4") == "AZURE_API_KEY"


def test_resolve_api_key_for_azure_reads_dedicated_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``azure/<deployment>`` → reads ``AZURE_API_KEY`` (LiteLLM-standard name)."""
    monkeypatch.delenv("AZURE_FOUNDRY_API_KEY", raising=False)
    monkeypatch.setenv("AZURE_API_KEY", "openai-azure-key-C")
    cfg = LLMConfig(model="azure/gpt-5.4")
    assert _resolve_api_key(cfg) == "openai-azure-key-C"


def test_resolve_api_key_for_azure_falls_back_to_foundry_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator who only set AZURE_FOUNDRY_API_KEY can still call ``azure/`` ids.

    Useful for the shared-tenant case where one Azure subscription serves both
    surfaces and the operator has a single key in their .env.
    """
    monkeypatch.delenv("AZURE_API_KEY", raising=False)
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "shared-key-D")
    cfg = LLMConfig(model="azure/gpt-5.4")
    assert _resolve_api_key(cfg) == "shared-key-D"


def test_ensure_bootstrapped_remaps_azure_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AZURE_ENDPOINT`` (operator-friendly name) → ``AZURE_API_BASE``
    (LiteLLM-standard name) at bootstrap.

    ``AZURE_API_KEY`` / ``AZURE_API_VERSION`` already match LiteLLM's expected
    names so they need no remap.
    """
    monkeypatch.delenv("AZURE_API_BASE", raising=False)
    monkeypatch.setenv("AZURE_ENDPOINT", "https://example.cognitiveservices.azure.com/")

    azure_foundry.ensure_bootstrapped()

    import os

    assert os.environ["AZURE_API_BASE"] == "https://example.cognitiveservices.azure.com/"


def test_ensure_bootstrapped_does_not_overwrite_explicit_api_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the operator explicitly sets ``AZURE_API_BASE``, we leave it alone
    even when ``AZURE_ENDPOINT`` is also set."""
    monkeypatch.setenv("AZURE_ENDPOINT", "https://endpoint.example/")
    monkeypatch.setenv("AZURE_API_BASE", "https://operator-explicit.example/")

    azure_foundry.ensure_bootstrapped()

    import os

    assert os.environ["AZURE_API_BASE"] == "https://operator-explicit.example/"


def test_ensure_bootstrapped_enables_litellm_drop_params() -> None:
    """gpt-5.x reasoning models reject ``max_tokens`` and want
    ``max_completion_tokens``. We set ``litellm.drop_params = True`` so the
    legacy parameter is auto-translated by LiteLLM rather than 400-ing."""
    azure_foundry.ensure_bootstrapped()
    import litellm

    assert litellm.drop_params is True


def test_ensure_bootstrapped_registers_deepseek_v4_pro_pricing() -> None:
    """LiteLLM ships without a price for ``azure/deepseek-v4-pro``. We
    register an ESTIMATED price at bootstrap so sandbox dev / test_implementer
    runs land non-zero cost in ``runs.cost_usd`` — otherwise the chain's spend
    caps are useless against the heaviest model.

    Test asserts:
      * The model id is registered.
      * Both per-token rates are present and > 0.
      * The metadata carries the ``factory_cost_note`` marker so anyone
        inspecting the cost map sees the values are estimates.
    """
    azure_foundry.ensure_bootstrapped()
    import litellm

    entry = litellm.model_cost.get("azure/deepseek-v4-pro")
    assert entry is not None, "azure/deepseek-v4-pro is unregistered after bootstrap"
    assert entry["input_cost_per_token"] > 0
    assert entry["output_cost_per_token"] > 0
    assert "ESTIMATED" in entry.get("factory_cost_note", "").upper(), (
        "ESTIMATED marker missing — operators must know prices are not exact"
    )


def test_deepseek_v4_pro_pricing_estimates_completion_cost() -> None:
    """LiteLLM's ``cost_per_token`` helper applies the registered rates.

    The registered rates are ESTIMATED ($0.50 / $1.50 per 1M tokens); we
    feed a known token count and assert the resulting cost is exactly
    what those rates predict. Anchors the registration end-to-end.
    """
    azure_foundry.ensure_bootstrapped()
    import litellm

    # 1M prompt + 1M completion → $0.50 input + $1.50 output = $2.00 total.
    prompt_cost, completion_cost = litellm.cost_per_token(
        model="azure/deepseek-v4-pro",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert prompt_cost == pytest.approx(0.50, rel=1e-6)
    assert completion_cost == pytest.approx(1.50, rel=1e-6)


# --------------------------------------------------------------------------- #
# routes.yaml + model_router integration
# --------------------------------------------------------------------------- #


def test_routes_yaml_default_provider_is_azure() -> None:
    """The shipped ``routes.yaml`` must declare azure as default."""
    import os

    # Clear any test-only override (e.g. ``test_model_router.py``'s fixture).
    os.environ.pop("FACTORY_PROVIDER", None)
    assert model_router.active_provider() == "azure"


def test_route_returns_azure_model_under_default_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``default_provider: azure`` every persona resolves to an
    ``azure/...`` model id (Azure-OpenAI surface)."""
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
        assert model_id.startswith("azure/"), f"{persona} routed to {model_id!r}"


def test_route_dev_uses_deepseek_v4_pro(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dev / test_implementer should route to deepseek-v4-pro (heavy impl)."""
    monkeypatch.delenv("FACTORY_PROVIDER", raising=False)
    assert model_router.route("dev", "standard") == "azure/deepseek-v4-pro"
    assert model_router.route("dev", "hard") == "azure/deepseek-v4-pro"
    assert model_router.route("test_implementer") == "azure/deepseek-v4-pro"


def test_route_text_personas_use_gpt_5_4(monkeypatch: pytest.MonkeyPatch) -> None:
    """Structured-text + code-judgment personas route to gpt-5.4.

    Code-judgment personas (reviewer / test_designer / security) were intended
    for ``gpt-5.3-codex`` but that deployment is Responses-API-only and the
    runner is Chat-Completions-only today. Falling back to gpt-5.4 keeps the
    reviewer ≠ dev invariant because dev / test_implementer run on
    deepseek-v4-pro.
    """
    monkeypatch.delenv("FACTORY_PROVIDER", raising=False)
    for persona in (
        "pm",
        "analyst",
        "sm",
        "tech_writer",
        "onboarder",
        "architect",
        "ralph",
        "bug_hunter",
        "release_manager",
        "reviewer",
        "test_designer",
        "security",
    ):
        assert model_router.route(persona) == "azure/gpt-5.4", persona


def test_factory_provider_env_override_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FACTORY_PROVIDER=direct`` overrides the YAML's ``default_provider``."""
    monkeypatch.setenv("FACTORY_PROVIDER", "direct")
    assert model_router.route("pm") == "deepseek/deepseek-chat"
    monkeypatch.setenv("FACTORY_PROVIDER", "azure")
    assert model_router.route("pm") == "azure/gpt-5.4"


def test_route_uses_azure_fallback_when_persona_missing(tmp_path: Path) -> None:
    """A persona absent from ``azure_routes`` falls back to ``azure_fallback``."""
    custom = tmp_path / "r.yaml"
    custom.write_text(
        "default_provider: azure\n"
        "azure_routes:\n"
        "  pm: azure/gpt-5.4\n"
        "defaults:\n"
        "  fallback: deepseek/deepseek-chat\n"
        "  azure_fallback: azure/gpt-5.4\n",
        encoding="utf-8",
    )
    assert model_router.route("nonexistent", routes_path=custom) == "azure/gpt-5.4"


# Silence the providers-import-only lint by reaching the module symbol.
_ = providers
