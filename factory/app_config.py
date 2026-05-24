"""Per-app configuration loader.

Each app lives at ``apps/<name>/`` in the factory repo and carries a
``config.yaml`` with its repo url, default branch, deploy commands, model
overrides, and context directory. The factory itself is app-agnostic — every
stack-specific value lives here.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class DeployConfig(BaseModel):
    enabled: bool = False
    pre_deploy_commands: list[str] = Field(default_factory=list)
    deploy_command: str | None = None
    health_check_command: str | None = None
    smoke_test_command: str | None = None
    rollback_command: str | None = None


class AppGatesConfig(BaseModel):
    """Per-app gate commands consumed by the auto-merge worker (Phase 4).

    Every field is optional: a missing command means "skip this gate". The
    factory itself is stack-agnostic — these strings are executed verbatim
    by the gate handler when the worker is in real-run mode, and only flag
    lookups are done in dry-run.
    """

    lint_command: str | None = None
    format_check_command: str | None = None
    type_check_command: str | None = None
    test_command: str | None = None
    coverage_command: str | None = None
    e2e_command: str | None = None
    mutation_testing: bool = False


class AppConfig(BaseModel):
    name: str
    repo: str  # "owner/name"
    default_branch: str = "main"
    context_dir: str = "context"
    deploy: DeployConfig = Field(default_factory=DeployConfig)
    gates: AppGatesConfig = Field(default_factory=AppGatesConfig)
    models: dict[str, str] = Field(default_factory=dict)  # persona overrides

    @property
    def repo_owner(self) -> str:
        return self.repo.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        return self.repo.split("/", 1)[1]


def load_app_config(app: str, software_factory_root: Path) -> AppConfig:
    """Load and validate ``apps/<app>/config.yaml`` from the factory root."""
    cfg_path = Path(software_factory_root) / "apps" / app / "config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"App config missing: {cfg_path}. Expected apps/<app>/config.yaml.")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{cfg_path}: top-level must be a YAML mapping")
    return AppConfig.model_validate(raw)
