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
    bug_hunter_runs_per_day: int = 2
    security_runs_per_day: int = 1
    ux_auditor_runs_per_day: int = 2
    # Factory self-improver cap. Cron fires it daily; the second slot
    # leaves room for a manual ``factory improve`` invocation.
    factory_improver_runs_per_day: int = 2


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


class AutoMergeConfig(BaseModel):
    """Controls the end-of-tick auto-merge worker.

    When ``enabled`` is true, ``orchestrator.tick`` calls
    ``auto_merge_tick`` after every story handler runs. ``trigger`` is
    reserved for future hooks (webhook-driven, scheduled) — currently
    only ``end_of_tick`` is honored.

    ``merge_method`` is passed through to ``gh pr merge`` (``squash`` |
    ``merge`` | ``rebase``). ``wait_for_ci`` adds ``--auto`` so GitHub
    holds the merge until required checks pass; for repos without
    required checks the merge happens immediately.
    """

    enabled: bool = False
    trigger: str = "end_of_tick"
    merge_method: str = "squash"
    delete_branch_after_merge: bool = True
    wait_for_ci: bool = True


class DevConvergenceConfig(BaseModel):
    """In-tick run-until-green convergence loop for the dev persona.

    When ``enabled``, a red dev run retries IMMEDIATELY inside the same
    ``handle_dev`` invocation (fresh sandbox, prior-attempts memory carried
    forward) instead of waiting for the next 5-minute tick — compressing
    N tick-gaps out of a story's convergence time. The loop never grants
    extra attempts: ``_MAX_DEV_RETRIES`` remains the single authoritative
    retry cap; this only changes WHEN the same retries happen.

    Guards (any failing guard stops the loop and falls back to the normal
    across-ticks path): ``max_inner_attempts`` per invocation, one retry of
    headroom under the chain cap, ``per_story_wall_clock_s`` elapsed,
    ``per_story_budget_usd`` spent by this story since the loop started,
    and a live re-check of the global hourly/daily spend caps (the settings
    enforcer only gates dispatch, so a tight loop must re-check mid-flight).
    """

    enabled: bool = False
    max_inner_attempts: int = 3
    per_story_wall_clock_s: int = 2700
    per_story_budget_usd: float = 8.0
    # Per-sandbox wall-clock passed to ``sandbox_run`` for dev; the module
    # default (1800s) stays in force when this matches it.
    dev_sandbox_timeout_s: int = 1800


class AutoPMSyncConfig(BaseModel):
    """Controls automatic PM triage of pending directions on every tick.

    When ``enabled``, ``factory tick`` runs the pm-sync pipeline whenever
    directions with status ``created``/``needs-direction`` exist, so work
    filed by the scheduled personas (ralph, bug_hunter, ux_auditor, …) or
    by ``factory tell`` flows into stories without an operator remembering
    to run ``factory pm-sync``. Bounded by
    ``rate_limits.pm_invocations_per_hour`` (counted from real ``pm`` rows
    in the runs table) so an erroring direction can't burn spend by being
    retriaged every tick.
    """

    enabled: bool = True


class FactorySettings(BaseModel):
    caps: CapsConfig = Field(default_factory=CapsConfig)
    queues: QueuesConfig = Field(default_factory=QueuesConfig)
    rate_limits: RateLimitsConfig = Field(default_factory=RateLimitsConfig)
    modes: ModesConfig = Field(default_factory=ModesConfig)
    direction_defaults: DirectionDefaults = Field(default_factory=DirectionDefaults)
    auto_merge: AutoMergeConfig = Field(default_factory=AutoMergeConfig)
    auto_pm_sync: AutoPMSyncConfig = Field(default_factory=AutoPMSyncConfig)
    dev_convergence: DevConvergenceConfig = Field(default_factory=DevConvergenceConfig)


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
