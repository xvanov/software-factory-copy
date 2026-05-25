"""Azure provider bootstrap (covers BOTH Foundry and Azure-OpenAI shapes).

Two distinct Azure surfaces are supported. They live in this module together
because they share env-remap mechanics and a single idempotent bootstrap
entry point; the wire shapes diverge entirely.

1. **Azure AI Foundry** (``services.ai.azure.com/models``-style endpoints)
   — addressed in LiteLLM via the ``azure_ai/`` prefix. Foundry exposes an
   OpenAI-compatible chat-completions endpoint at
   ``<base>/chat/completions?api-version=<version>``. LiteLLM auto-downgrades
   to the Azure-OpenAI deployment URL whenever the model name (e.g.
   ``gpt-4.1``) appears in ``litellm.open_ai_chat_completion_models``; we
   monkey-patch that detection off so every ``azure_ai/...`` id uses the
   simpler OpenAI-compatible path.

2. **Azure OpenAI / Cognitive Services** (``cognitiveservices.azure.com``
   endpoints with named deployments) — addressed in LiteLLM via the plain
   ``azure/`` prefix. Each deployment is reached at
   ``<base>/openai/deployments/<deployment>/chat/completions?api-version=X``.
   LiteLLM reads ``AZURE_API_BASE`` / ``AZURE_API_KEY`` / ``AZURE_API_VERSION``
   from the environment automatically — we only need to remap the friendlier
   ``AZURE_ENDPOINT`` name into ``AZURE_API_BASE``.

``ensure_bootstrapped()`` is idempotent and is called at the start of every
Azure-bound runner code path. It:

  1. Remaps ``AZURE_FOUNDRY_*`` env vars → LiteLLM's ``AZURE_AI_API_*`` names
     (Foundry path).
  2. Remaps ``AZURE_ENDPOINT`` → ``AZURE_API_BASE`` (Azure-OpenAI path).
     ``AZURE_API_KEY`` / ``AZURE_API_VERSION`` already match LiteLLM's
     expected names and are left as-is.
  3. Sets ``litellm.drop_params = True`` so newer reasoning-class models
     (``gpt-5.4`` etc.) that reject the legacy ``max_tokens`` get it
     translated to ``max_completion_tokens`` instead of 400-ing.
  4. Monkey-patches LiteLLM's Foundry detection. This is a no-op for the
     plain ``azure/`` path; it only affects ``azure_ai/`` calls.

We never overwrite a value the operator set explicitly.
"""

from __future__ import annotations

import os

# Track whether bootstrap ran in this process so test code can re-invoke it.
_bootstrapped: bool = False


def _remap_env() -> None:
    """Copy operator-friendly env-var names into LiteLLM-expected ones.

    Two independent remaps:

    * ``AZURE_FOUNDRY_*`` → ``AZURE_AI_API_*`` (Foundry / ``azure_ai/`` path).
    * ``AZURE_ENDPOINT`` → ``AZURE_API_BASE`` (Azure-OpenAI / ``azure/`` path).

    ``AZURE_API_KEY`` / ``AZURE_API_VERSION`` are already LiteLLM-standard
    names and are left untouched.

    We never overwrite a destination that is already set explicitly.
    """
    pairs = [
        # Foundry (azure_ai/...) path
        ("AZURE_FOUNDRY_ENDPOINT", "AZURE_AI_API_BASE"),
        ("AZURE_FOUNDRY_API_KEY", "AZURE_AI_API_KEY"),
        ("AZURE_FOUNDRY_API_VERSION", "AZURE_AI_API_VERSION"),
        # Azure-OpenAI (azure/...) path — operator-friendly alias for AZURE_API_BASE
        ("AZURE_ENDPOINT", "AZURE_API_BASE"),
    ]
    for src, dst in pairs:
        src_val = os.environ.get(src)
        if src_val and not os.environ.get(dst):
            os.environ[dst] = src_val


def _patch_litellm_azure_ai() -> None:
    """Force LiteLLM's ``azure_ai/`` provider to use the OpenAI-compatible path.

    Without this patch, ``azure_ai/gpt-4.1`` is detected as Azure OpenAI and
    routed to ``<base>/openai/deployments/<name>/chat/completions``, which
    does not exist on a Foundry endpoint. The override forces the simpler
    ``<base>/chat/completions?api-version=X`` path.

    Affects ONLY ``azure_ai/...`` calls. The plain ``azure/...`` path is
    untouched and continues to use the deployment-name URL it expects.
    """
    try:
        from litellm.llms.azure_ai.chat.transformation import AzureAIStudioConfig
    except Exception:  # pragma: no cover — LiteLLM should always be importable
        return

    AzureAIStudioConfig._is_azure_openai_model = (  # type: ignore[method-assign]
        lambda self, model, api_base: False
    )


def _enable_litellm_drop_params() -> None:
    """Make LiteLLM silently drop / translate provider-unsupported params.

    ``gpt-5.4`` (and other 2026 reasoning-class models) reject the legacy
    ``max_tokens`` parameter and want ``max_completion_tokens`` instead. With
    ``drop_params=True``, LiteLLM auto-translates rather than 400-ing, so
    callers can keep passing ``max_tokens`` uniformly.
    """
    try:
        import litellm

        litellm.drop_params = True
    except Exception:  # pragma: no cover
        return


# Estimated per-token costs for ``azure/deepseek-v4-pro``. LiteLLM ships
# without a pricing entry for this Azure deployment, so every sandbox dev /
# test_implementer run lands a ``cost=0.0`` row in ``state/factory.db.runs``
# even though the model burns 700K+ tokens per call. Without a price, the
# chain's spend caps are useless against the heaviest model.
#
# These rates are an estimate anchored on DeepSeek-V3 list pricing
# (~$0.27 / $1.10 per 1M) with a typical Azure deployment markup applied.
# UPDATE from real Azure billing data when available — search for this
# constant and bump it. The registered metadata also tags the entry with
# ``factory_cost_note`` so anyone inspecting ``litellm.model_cost`` sees the
# caveat directly.
_DEEPSEEK_V4_PRO_INPUT_PER_TOKEN = 0.0000005  # $0.50 per 1M (ESTIMATED)
_DEEPSEEK_V4_PRO_OUTPUT_PER_TOKEN = 0.0000015  # $1.50 per 1M (ESTIMATED)


def _register_litellm_pricing() -> None:
    """Register cost-per-token entries for Azure deployments LiteLLM doesn't know.

    Currently registers ``azure/deepseek-v4-pro`` only; the other deployments
    on the resource (``azure/gpt-5.4``) get prices from LiteLLM's built-in
    table. Re-registering an already-known model is a no-op for the price
    fields; LiteLLM simply overwrites them.

    The pricing values are flagged as ESTIMATED in the metadata — the source
    of truth is the constants above this function.
    """
    try:
        import litellm

        litellm.register_model(
            {
                "azure/deepseek-v4-pro": {
                    "input_cost_per_token": _DEEPSEEK_V4_PRO_INPUT_PER_TOKEN,
                    "output_cost_per_token": _DEEPSEEK_V4_PRO_OUTPUT_PER_TOKEN,
                    "litellm_provider": "azure",
                    "mode": "chat",
                    # Marker so anyone inspecting the cost map sees the caveat.
                    "factory_cost_note": "ESTIMATED — verify against Azure billing",
                },
            }
        )
    except Exception:  # pragma: no cover — LiteLLM should always be importable
        return


def ensure_bootstrapped() -> None:
    """Idempotent bootstrap. Call before any Azure-bound LLM completion."""
    global _bootstrapped
    if _bootstrapped:
        return
    _remap_env()
    _patch_litellm_azure_ai()
    _enable_litellm_drop_params()
    _register_litellm_pricing()
    _bootstrapped = True


def reset_for_tests() -> None:
    """Test-only: re-allow bootstrap to run again (env may have changed)."""
    global _bootstrapped
    _bootstrapped = False
