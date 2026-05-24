"""Azure AI Foundry provider bootstrap.

Azure AI Foundry exposes an **OpenAI-compatible** chat-completions endpoint
at ``<base>/chat/completions?api-version=<version>``. LiteLLM has built-in
support for this via the ``azure_ai/`` provider prefix, but it auto-downgrades
to the Azure-OpenAI path whenever the model name (e.g. ``gpt-4.1``) appears
in ``litellm.open_ai_chat_completion_models``. The Azure-OpenAI path builds
``<base>/openai/deployments/<name>/chat/completions?api-version=X``, which
does NOT exist on a Foundry endpoint.

This module:

  1. Remaps user-facing ``AZURE_FOUNDRY_*`` env vars to the LiteLLM-expected
     ``AZURE_AI_API_*`` names.
  2. Monkey-patches ``AzureAIStudioConfig._is_azure_openai_model`` so the
     OpenAI-compatible path is used for **every** ``azure_ai/...`` model id,
     including ``gpt-4.1``. The patch is a no-op when the SDK isn't loaded.

``ensure_bootstrapped()`` is idempotent and safe to call repeatedly.
"""

from __future__ import annotations

import os

# Track whether bootstrap ran in this process so test code can re-invoke it.
_bootstrapped: bool = False


def _remap_env() -> None:
    """Copy ``AZURE_FOUNDRY_*`` into LiteLLM's ``AZURE_AI_API_*`` names.

    We never overwrite a value that is already set explicitly.
    """
    pairs = [
        ("AZURE_FOUNDRY_ENDPOINT", "AZURE_AI_API_BASE"),
        ("AZURE_FOUNDRY_API_KEY", "AZURE_AI_API_KEY"),
        ("AZURE_FOUNDRY_API_VERSION", "AZURE_AI_API_VERSION"),
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
    """
    try:
        from litellm.llms.azure_ai.chat.transformation import AzureAIStudioConfig
    except Exception:  # pragma: no cover — LiteLLM should always be importable
        return

    AzureAIStudioConfig._is_azure_openai_model = (  # type: ignore[method-assign]
        lambda self, model, api_base: False
    )


def ensure_bootstrapped() -> None:
    """Idempotent bootstrap. Call before any ``azure_ai/...`` LLM completion."""
    global _bootstrapped
    if _bootstrapped:
        return
    _remap_env()
    _patch_litellm_azure_ai()
    _bootstrapped = True


def reset_for_tests() -> None:
    """Test-only: re-allow bootstrap to run again (env may have changed)."""
    global _bootstrapped
    _bootstrapped = False
