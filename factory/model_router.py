"""Persona-aware LiteLLM model routing.

The router reads ``factory/routes.yaml`` (or any path passed in) and returns a
LiteLLM model id for a given persona + difficulty. Routes are intentionally a
plain YAML file so an agent in a later phase can edit them — this is the
self-improvement seam.
"""

from __future__ import annotations

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


def route(persona: str, difficulty: str = "standard", *, routes_path: Path | None = None) -> str:
    """Return the LiteLLM model id for a persona+difficulty.

    Resolution order:
      1. ``routes.<persona>`` is a mapping → use ``routes.<persona>[difficulty]``;
         if missing, try ``routes.<persona>["standard"]``; else fallback.
      2. ``routes.<persona>`` is a string → return it (difficulty ignored).
      3. Otherwise → ``defaults.fallback``.

    Raises ``KeyError`` if no route AND no fallback is configured.
    """
    data = _load_routes(routes_path)
    routes_section = data.get("routes", {}) or {}
    fallback = (data.get("defaults", {}) or {}).get("fallback")

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
        f"and no defaults.fallback set"
    )


def all_known_personas(*, routes_path: Path | None = None) -> list[str]:
    """Return all persona keys defined in routes.yaml. Useful for CLI listings."""
    data = _load_routes(routes_path)
    return sorted((data.get("routes") or {}).keys())
