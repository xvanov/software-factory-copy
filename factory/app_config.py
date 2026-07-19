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
    """Per-app deploy block consumed by ``factory/deploy/orchestrator.py``.

    Every command is an opaque shell string the factory passes verbatim to
    a subprocess. The factory itself is stack-agnostic — it knows nothing
    about Docker, Compose, Fly, Vercel, etc. Apps declare commands here;
    Phase 5's orchestrator executes them in a fixed sequence.
    """

    enabled: bool = False
    pre_deploy_commands: list[str] = Field(default_factory=list)
    deploy_command: str | None = None
    health_check_command: str | None = None
    health_check_max_attempts: int = 5
    health_check_interval_seconds: int = 5
    smoke_test_command: str | None = None
    rollback_command: str | None = None
    # Optional metadata commands (label -> shell). Run after a successful
    # deploy; their stdout is captured into DeployActionRecord for audit
    # (e.g. ``docker compose ps --format json`` to record container state).
    post_deploy_record: dict[str, str] = Field(default_factory=dict)
    # Per-command working directory (relative to the cloned app repo
    # root). Phase 5 dry-run ignores this entirely; real-run resolves it
    # against the app workspace. None means the factory root.
    working_directory: str | None = None
    # Env vars from the factory process forwarded to the deploy
    # subprocess. PATH is always forwarded; everything else is opt-in.
    env_var_passthrough: list[str] = Field(default_factory=list)
    # Subprocess timeout per command (seconds).
    timeout_seconds: int = 600


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
    # Whether a WORKING end-to-end/browser harness actually exists in the app
    # (Playwright installed + config + the stack runnable in the sandbox). A
    # configured ``e2e_command`` does NOT imply this — sacrifice declares
    # ``npx playwright test`` but has no runnable harness, so test_designer
    # mandating Playwright produced harness-breakage "reds" that deadlocked
    # every frontend/E2E story. When False, the test_designer must NOT require
    # E2E/Playwright and should scope to the backend test_command instead.
    e2e_harness_ready: bool = False

    # Runtime smoke gate (Karpathy Layer-2 "external signal", D002). A command
    # that BOOTS the running product and exercises one real user journey
    # (e.g. docker compose up + a scripted sign-up → login → core-action pass).
    # Distinct from ``test_command`` (unit/integration, app never starts) and
    # from ``e2e_command`` (declared but historically unrunnable). The factory
    # shipped a full backlog green while the app could not log in precisely
    # because nothing booted it; this gate is the oracle that closes that class.
    smoke_command: str | None = None
    # Whether a WORKING smoke harness actually exists (stack runnable in the
    # sandbox + the scripted journey passes). Only when True does ``smoke-green``
    # become a merge-REQUIRED gate for this app — keeping the rollout per-app
    # opt-in so apps without a harness are unaffected (no new merge blocks,
    # avoiding the PRs 110/111 "every merge blocked" regression).
    smoke_harness_ready: bool = False

    # WS1.2 independent acceptance oracle. When True, the chain authors an
    # acceptance test from each story's direction acceptance criteria (the SPEC
    # ONLY, blind to the dev's code/tests) at spawn time, stores it in factory
    # state OUTSIDE the dev worktree, and the ``acceptance-verified`` gate copies
    # it into the merge-candidate checkout and runs it as a REQUIRED gate. Off by
    # default so the rollout is per-app opt-in — an app that hasn't enabled it
    # sees no new merge blocks (mirrors the ``smoke_harness_ready`` rollout). The
    # gate is required only for stories that actually got an oracle authored
    # (ACs present + this flag on); legacy / no-AC stories are never blocked.
    acceptance_oracle: bool = False
    # Command template the acceptance gate runs, with ``{test_file}`` substituted
    # for the copied-in test's path (relative to the checkout root). Defaults to
    # ``python -m pytest {test_file} -q`` when unset; apps whose suite needs a
    # wrapper (e.g. ``uv run pytest {test_file} -q``) override it here so the
    # oracle runs against the app's real python env.
    acceptance_test_command: str | None = None


class AppConfig(BaseModel):
    name: str
    repo: str  # "owner/name"
    default_branch: str = "main"
    context_dir: str = "context"
    # Path to the actual app source tree, relative to the factory root.
    # Default ``../<name>`` matches the convention "factory at
    # ``~/software-factory/``, apps at ``~/<name>/`` (siblings)". Personas
    # read context from this path, NOT from ``apps/<name>/`` inside the
    # factory (which only holds the per-app config + directions + state).
    app_repo_path: str = ""
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
    """Load and validate ``apps/<app>/config.yaml`` from the factory root.

    If ``app_repo_path`` is unset in the YAML, it defaults to ``../<name>``
    relative to the factory root (e.g. factory at ``~/software-factory/``
    and apps as sibling directories at ``~/<name>/``).
    """
    cfg_path = Path(software_factory_root) / "apps" / app / "config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"App config missing: {cfg_path}. Expected apps/<app>/config.yaml.")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{cfg_path}: top-level must be a YAML mapping")
    if not raw.get("app_repo_path"):
        # Mirror the documented convention: app source lives at a sibling
        # of the factory root, named by the app.
        raw["app_repo_path"] = f"../{raw.get('name') or app}"
    return AppConfig.model_validate(raw)


def resolve_app_repo_path(cfg: AppConfig, software_factory_root: Path) -> Path:
    """Resolve ``cfg.app_repo_path`` against the factory root.

    Absolute paths are returned unchanged; relative paths are anchored at
    ``software_factory_root``. The result is NOT required to exist —
    callers handle the "no app tree yet" case (e.g. context loader emits
    the NO CONTEXT AVAILABLE notice).
    """
    raw = (cfg.app_repo_path or "").strip()
    if not raw:
        raw = f"../{cfg.name}"
    p = Path(raw)
    if p.is_absolute():
        return p
    return (Path(software_factory_root) / p).resolve()
