"""Load and validate ``factory_settings.yaml``.

The settings file is the **global** dial: caps (concurrency, spend),
mode set, rate limits, and direction defaults. The runtime *mode* is
mutable state and lives in ``state/factory.db.factory_state``, not in
this YAML — the YAML only declares which mode names are allowed.

A missing file resolves to the documented defaults so a fresh checkout
boots without yelling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class CapsConfig(BaseModel):
    global_concurrent_agents: int = 2
    per_repo_concurrent_agents: int = 2
    daily_spend_usd: float = 10.0
    hourly_spend_usd: float = 2.0


class QueuesConfig(BaseModel):
    human_review_max_open_prs: int = 5
    failing_ci_pause_threshold: int = 3


class RateLimitsConfig(BaseModel):
    pm_invocations_per_hour: int = 4
    ralph_runs_per_day: int = 24


class ModesConfig(BaseModel):
    default: str = "normal"
    available: list[str] = Field(
        default_factory=lambda: [
            "normal",
            "fix-only",
            "drain-reviews",
            "paused",
            "exploratory",
            "deploy-frozen",
            "ux-audit-only",
        ]
    )


class DirectionDefaults(BaseModel):
    require_user_flow_for_ui: bool = True
    require_api_spec_for_backend: bool = True
    allow_explore_tag: bool = True
    max_dev_retries: int = 3
    escalate_model_on_retry: bool = True
    require_context_update_per_pr: bool = True
    enforce_canonical_doc_paths: bool = True


class FactorySettings(BaseModel):
    caps: CapsConfig = Field(default_factory=CapsConfig)
    queues: QueuesConfig = Field(default_factory=QueuesConfig)
    rate_limits: RateLimitsConfig = Field(default_factory=RateLimitsConfig)
    modes: ModesConfig = Field(default_factory=ModesConfig)
    direction_defaults: DirectionDefaults = Field(default_factory=DirectionDefaults)


_CACHED: dict[Path, FactorySettings] = {}


def load_settings(software_factory_root: Path) -> FactorySettings:
    """Read ``factory_settings.yaml`` at the root; missing file -> defaults.

    Parsed objects are memoized per-root for the life of the process; call
    ``reload_settings(...)`` after mutating the YAML in a test.
    """
    root = Path(software_factory_root).resolve()
    if root in _CACHED:
        return _CACHED[root]
    path = root / "factory_settings.yaml"
    settings: FactorySettings
    if not path.exists():
        settings = FactorySettings()
    else:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: top-level must be a YAML mapping")
        settings = FactorySettings.model_validate(raw)
        if settings.modes.default not in settings.modes.available:
            raise ValueError(
                f"{path}: modes.default={settings.modes.default!r} is not in "
                f"modes.available={settings.modes.available!r}"
            )
    _CACHED[root] = settings
    return settings


def reload_settings(software_factory_root: Path) -> FactorySettings:
    """Bust the cache for ``software_factory_root`` and re-read the YAML."""
    root = Path(software_factory_root).resolve()
    _CACHED.pop(root, None)
    return load_settings(root)


def is_valid_mode(mode_name: str, settings: FactorySettings) -> bool:
    """True iff ``mode_name`` is in ``settings.modes.available``."""
    return mode_name in settings.modes.available
