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

import os
from pathlib import Path
from typing import Any, cast

import yaml

_DEFAULT_ROUTES_PATH = Path(__file__).parent / "routes.yaml"


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

    Raises ``KeyError`` if no route AND no fallback is configured.
    """
    data = _load_routes(routes_path)
    provider = _active_provider(data)
    routes_section, fallback = _routes_section_for(provider, data)

    entry = routes_section.get(persona)
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        diff_val = entry.get(difficulty)
        if isinstance(diff_val, str):
            return diff_val
        std_val = entry.get("standard")
        if isinstance(std_val, str):
            return std_val
        # mapping exists but neither difficulty nor standard present — fall through
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
