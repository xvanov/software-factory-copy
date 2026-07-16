"""Persona-aware LiteLLM model routing.

The router reads ``factory/routes.yaml`` (or any path passed in) and returns a
LiteLLM model id for a given persona + difficulty. Routes are intentionally a
plain YAML file so an agent in a later phase can edit them — this is the
self-improvement seam.

Phase 8 — Azure provider support
--------------------------------
``routes.yaml`` may now declare ``default_provider: azure | direct`` and a
separate ``azure_routes:`` block. At runtime the router picks the active
routes block via this precedence:

  1. ``FACTORY_PROVIDER`` env var (``azure`` or ``direct``) — runtime override.
  2. ``default_provider`` from ``routes.yaml``.
  3. ``"direct"`` if neither is set (back-compat).

Tests can inject ``FACTORY_PROVIDER`` to flip providers without touching the
YAML; humans flip the YAML and commit it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, cast

import yaml

_DEFAULT_ROUTES_PATH = Path(__file__).parent / "routes.yaml"

_log = logging.getLogger(__name__)

# Model-id prefix → env var holding that provider's API key. Single source of
# truth — ``factory.runner._provider_env_key`` delegates here.
_PROVIDER_ENV_KEYS: tuple[tuple[str, str], ...] = (
    ("openrouter/", "OPENROUTER_API_KEY"),
    ("deepseek/", "DEEPSEEK_API_KEY"),
    ("anthropic/", "ANTHROPIC_API_KEY"),
    ("openai/", "OPENAI_API_KEY"),
    ("azure_ai/", "AZURE_AI_API_KEY"),
    ("azure/", "AZURE_API_KEY"),
)


def provider_env_key(model: str) -> str | None:
    """Return the env-var name that holds the API key for ``model``."""
    for prefix, env_name in _PROVIDER_ENV_KEYS:
        if model.startswith(prefix):
            return env_name
    if model.startswith("claude"):
        return "ANTHROPIC_API_KEY"
    if model.startswith("gpt"):
        return "OPENAI_API_KEY"
    return None


def _provider_key_available(model: str) -> bool:
    """True when the provider key for ``model`` is present and non-empty.

    Unknown prefixes return True — the runner owns final key resolution and
    error reporting; the router only degrades *known-missing* providers.
    """
    env_name = provider_env_key(model)
    if env_name is None:
        return True
    if os.environ.get(env_name):
        return True
    # The runner accepts AZURE_FOUNDRY_API_KEY as a fallback for both Azure
    # surfaces (see factory/runner.py::_resolve_api_key) — mirror that here.
    if env_name in ("AZURE_AI_API_KEY", "AZURE_API_KEY") and os.environ.get(
        "AZURE_FOUNDRY_API_KEY"
    ):
        return True
    return False


# Warn once per (persona, model) so a 60s-cadence caller (L1 watcher) doesn't
# spam the log with an identical degradation notice every tick.
_KEY_FALLBACK_WARNED: set[tuple[str, str]] = set()


def _load_routes(path: Path | None = None) -> dict[str, Any]:
    routes_path = path or _DEFAULT_ROUTES_PATH
    with routes_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{routes_path} must be a YAML mapping at top level")
    return cast(dict[str, Any], data)


def _active_provider(data: dict[str, Any]) -> str:
    env_override = os.environ.get("FACTORY_PROVIDER")
    if env_override:
        return env_override.strip().lower()
    yaml_default = data.get("default_provider")
    if isinstance(yaml_default, str) and yaml_default.strip():
        return yaml_default.strip().lower()
    return "direct"


def _routes_section_for(provider: str, data: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Return the (routes_mapping, fallback) pair for ``provider``."""
    defaults = data.get("defaults", {}) or {}
    if provider == "azure":
        return (
            data.get("azure_routes", {}) or {},
            defaults.get("azure_fallback") or defaults.get("fallback"),
        )
    return (data.get("routes", {}) or {}, defaults.get("fallback"))


def route(persona: str, difficulty: str = "standard", *, routes_path: Path | None = None) -> str:
    """Return the LiteLLM model id for a persona+difficulty.

    Resolution order (after provider selection — see module docstring):
      1. ``<active_routes>.<persona>`` is a mapping → use
         ``<active_routes>.<persona>[difficulty]``; if missing, try
         ``<active_routes>.<persona>["standard"]``; else fallback.
      2. ``<active_routes>.<persona>`` is a string → return it (difficulty ignored).
      3. Otherwise → ``defaults.fallback`` (or ``defaults.azure_fallback`` for azure).

    Key-aware degradation: if the resolved model's provider API key is absent
    from the environment but the fallback model's key IS available, the
    fallback is returned instead (warned once per persona+model). This keeps a
    freshly-rerouted routes.yaml safe to ship before every provider key is
    provisioned — the intended route activates automatically once its key
    lands in ``.env``.

    Raises ``KeyError`` if no route AND no fallback is configured.
    """
    data = _load_routes(routes_path)
    provider = _active_provider(data)
    routes_section, fallback = _routes_section_for(provider, data)

    resolved: str | None = None
    entry = routes_section.get(persona)
    if isinstance(entry, str):
        resolved = entry
    elif isinstance(entry, dict):
        diff_val = entry.get(difficulty)
        if isinstance(diff_val, str):
            resolved = diff_val
        else:
            std_val = entry.get("standard")
            if isinstance(std_val, str):
                resolved = std_val
            # mapping exists but neither difficulty nor standard present — fall through

    if resolved is not None:
        if (
            isinstance(fallback, str)
            and fallback != resolved
            and not _provider_key_available(resolved)
            and _provider_key_available(fallback)
        ):
            if (persona, resolved) not in _KEY_FALLBACK_WARNED:
                _KEY_FALLBACK_WARNED.add((persona, resolved))
                _log.warning(
                    "route(%s): provider key %s for %r is not set — "
                    "degrading to fallback %r until the key is provisioned",
                    persona,
                    provider_env_key(resolved),
                    resolved,
                    fallback,
                )
            return fallback
        return resolved
    if isinstance(fallback, str):
        return fallback
    raise KeyError(
        f"No route configured for persona={persona!r} difficulty={difficulty!r} "
        f"under provider={provider!r} and no fallback set"
    )


def all_known_personas(*, routes_path: Path | None = None) -> list[str]:
    """Return all persona keys defined in the active routes block.

    Useful for CLI listings — falls back to merging both blocks if the active
    one is empty (defensive: a half-configured YAML shouldn't render the CLI
    empty-handed).
    """
    data = _load_routes(routes_path)
    provider = _active_provider(data)
    routes_section, _ = _routes_section_for(provider, data)
    if not routes_section:
        merged: dict[str, Any] = {}
        merged.update(data.get("routes", {}) or {})
        merged.update(data.get("azure_routes", {}) or {})
        routes_section = merged
    return sorted(routes_section.keys())


def active_provider(*, routes_path: Path | None = None) -> str:
    """Public accessor — returns the provider the router is currently using."""
    return _active_provider(_load_routes(routes_path))


# Params that may flow from routes.yaml's ``llm_params`` block into the
# OpenHands SDK ``LLM(...)`` constructor. Anything else in the YAML is
# silently dropped — a config typo must not be able to crash LLM construction.
_ALLOWED_LLM_PARAMS = frozenset(
    {
        "reasoning_effort",
        "caching_prompt",
        "max_output_tokens",
        "temperature",
        "litellm_extra_body",
    }
)

_KNOWN_DIFFICULTIES = ("standard", "hard")


def llm_params_for(
    persona: str,
    model_id: str,
    *,
    difficulty: str = "standard",
    routes_path: Path | None = None,
) -> dict[str, Any]:
    """Per-persona LLM constructor overrides from routes.yaml's ``llm_params``.

    Merge order (later wins):
      1. ``{"max_output_tokens": max_output_tokens_for(model_id)}``
      2. ``llm_params.defaults``
      3. ``llm_params.personas.<persona>`` — either a flat param mapping or a
         difficulty-keyed mapping of param mappings (same shape convention as
         ``routes.<persona>``).

    Output is allowlisted to ``_ALLOWED_LLM_PARAMS``; unknown keys are dropped.
    """
    data = _load_routes(routes_path)
    merged: dict[str, Any] = {
        "max_output_tokens": max_output_tokens_for(model_id, routes_path=routes_path)
    }

    block = data.get("llm_params", {}) or {}
    defaults = block.get("defaults", {}) or {}
    if isinstance(defaults, dict):
        merged.update(defaults)

    personas = block.get("personas", {}) or {}
    entry = personas.get(persona) if isinstance(personas, dict) else None
    if isinstance(entry, dict):
        # Difficulty-keyed iff its keys are difficulty names mapping to dicts
        # (param names like ``reasoning_effort`` can never collide with those).
        if any(k in entry for k in _KNOWN_DIFFICULTIES):
            diff_entry = entry.get(difficulty) or entry.get("standard") or {}
            if isinstance(diff_entry, dict):
                merged.update(diff_entry)
        else:
            merged.update(entry)

    return {k: v for k, v in merged.items() if k in _ALLOWED_LLM_PARAMS}


def preset_for(persona: str, *, routes_path: Path | None = None) -> str | None:
    """Agent-preset override for ``persona`` from routes.yaml's ``presets``
    block (e.g. ``dev: planning``). ``None`` means the default agent preset."""
    data = _load_routes(routes_path)
    presets = data.get("presets", {}) or {}
    value = presets.get(persona) if isinstance(presets, dict) else None
    return value if isinstance(value, str) and value.strip() else None


# Built-in fallback if neither ``model_limits`` nor
# ``defaults_extra.max_output_tokens_default`` are defined in routes.yaml.
# Picked to cover the smallest current-fleet cap (DeepSeek-V4 native 8k).
_HARD_FALLBACK_MAX_OUTPUT_TOKENS = 8192


def max_output_tokens_for(model_id: str, *, routes_path: Path | None = None) -> int:
    """Return the per-call ``max_tokens`` (= max completion / output) cap
    for ``model_id``.

    Looked up by the LiteLLM model id returned by ``route()``. The value
    is the model's stated output ceiling — distinct from its context
    window (which governs input size).

    Resolution order:
      1. ``model_limits.<model_id>.max_output_tokens`` in routes.yaml.
      2. ``defaults_extra.max_output_tokens_default`` in routes.yaml.
      3. Hard-coded fallback (8192) — covers the smallest current-fleet cap.

    Providers bill by actual tokens used, not by the cap, so generous
    values cost nothing on outputs that fit comfortably. Too-low caps
    cause truncation mid-JSON and downstream JSONDecodeError.
    """
    data = _load_routes(routes_path)
    limits = data.get("model_limits", {}) or {}
    entry = limits.get(model_id)
    if isinstance(entry, dict):
        val = entry.get("max_output_tokens")
        if isinstance(val, int) and val > 0:
            return val
    defaults_extra = data.get("defaults_extra", {}) or {}
    default_val = defaults_extra.get("max_output_tokens_default")
    if isinstance(default_val, int) and default_val > 0:
        return default_val
    return _HARD_FALLBACK_MAX_OUTPUT_TOKENS
