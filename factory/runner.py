"""Persona runners.

Two entry points:

* ``sandbox_run`` — launches an OpenHands SDK ``Conversation`` against a real
  repo on local disk. Used for personas that need to read/write code (Dev,
  Test-Implementer, Onboarder, Reviewer-in-repo-mode, etc.).

* ``text_run`` — single ``litellm.completion()`` call with no tools. Used for
  text-only personas (PM classification, Reviewer-of-diff, Tech-Writer
  patches, etc.). Supports JSON-schema validation.

Both runners record a row in ``state/factory.db.runs`` keyed on persona +
timestamp + token usage + cost.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlmodel import Field, Session, SQLModel, create_engine

# Heavy SDK imports are deferred to inside sandbox_run() so the CLI can import
# this module without paying the OpenHands SDK import cost (and so tests that
# don't touch sandbox_run never need OpenHands installed).

_DEFAULT_DB_PATH = Path(__file__).parent.parent / "state" / "factory.db"
_PERSONAS_DIR = Path(__file__).parent / "personas"


# --------------------------------------------------------------------------- #
# Public dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LLMConfig:
    """Minimal LLM config the runner needs.

    ``model`` is a LiteLLM-format id (e.g. ``deepseek/deepseek-coder``).
    ``api_key`` may be None — in that case the runner falls back to the
    appropriate provider env var (``DEEPSEEK_API_KEY``, ``ANTHROPIC_API_KEY``,
    ``OPENAI_API_KEY``).
    ``base_url`` overrides the provider default.
    """

    model: str
    api_key: str | None = None
    base_url: str | None = None


@dataclass
class RunResult:
    success: bool
    files_changed: list[str] = field(default_factory=list)
    test_run_passed: bool | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    summary: str = ""


# --------------------------------------------------------------------------- #
# DB model
# --------------------------------------------------------------------------- #


class Run(SQLModel, table=True):
    __tablename__ = "runs"

    id: int | None = Field(default=None, primary_key=True)
    ts: str
    persona: str
    model: str
    mode: str  # "sandbox" | "text" | "sandbox-dry-run"
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    success: bool = False
    story_path: str | None = None
    repo_path: str | None = None
    error: str | None = None


def _engine(db_path: Path | None = None) -> Any:
    path = db_path or _DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", echo=False)
    SQLModel.metadata.create_all(engine)
    return engine


def _record_run(
    *,
    persona: str,
    model: str,
    mode: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    success: bool,
    story_path: str | None,
    repo_path: str | None,
    error: str | None,
    db_path: Path | None = None,
) -> None:
    engine = _engine(db_path)
    with Session(engine) as session:
        row = Run(
            ts=datetime.now(UTC).isoformat(),
            persona=persona,
            model=model,
            mode=mode,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            success=success,
            story_path=story_path,
            repo_path=repo_path,
            error=error,
        )
        session.add(row)
        session.commit()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _read_persona_prompt(persona: str) -> str:
    path = _PERSONAS_DIR / f"{persona}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"Persona file missing: {path}. Available: "
            f"{sorted(p.stem for p in _PERSONAS_DIR.glob('*.md'))}"
        )
    return path.read_text(encoding="utf-8")


def _provider_env_key(model: str) -> str | None:
    """Return the env-var name that holds the API key for ``model``.

    Two Azure prefixes are kept distinct:

    * ``azure_ai/...``  → Azure AI Foundry          → ``AZURE_AI_API_KEY``
    * ``azure/...``     → Azure OpenAI / Cognitive  → ``AZURE_API_KEY``

    The two surfaces share neither URL shape nor key scope.
    """
    if model.startswith("deepseek/"):
        return "DEEPSEEK_API_KEY"
    if model.startswith("anthropic/") or model.startswith("claude"):
        return "ANTHROPIC_API_KEY"
    if model.startswith("openai/") or model.startswith("gpt"):
        return "OPENAI_API_KEY"
    if model.startswith("azure_ai/"):
        return "AZURE_AI_API_KEY"
    if model.startswith("azure/"):
        return "AZURE_API_KEY"
    return None


def _resolve_api_key(cfg: LLMConfig) -> str | None:
    # Bootstrap Azure env remap on first resolution. Covers BOTH surfaces:
    #   * AZURE_FOUNDRY_* → AZURE_AI_API_* (Foundry / azure_ai)
    #   * AZURE_ENDPOINT  → AZURE_API_BASE (Azure-OpenAI / azure)
    # ...and sets ``litellm.drop_params = True`` so gpt-5.x reasoning models
    # accept ``max_tokens`` (auto-translated to ``max_completion_tokens``).
    from factory.providers.azure_foundry import ensure_bootstrapped

    ensure_bootstrapped()
    if cfg.api_key:
        return cfg.api_key
    env_key = _provider_env_key(cfg.model)
    if env_key:
        value = os.environ.get(env_key)
        if value:
            return value
        # Fallbacks for the two Azure surfaces — accept legacy names too.
        if env_key == "AZURE_AI_API_KEY":
            return os.environ.get("AZURE_FOUNDRY_API_KEY")
        if env_key == "AZURE_API_KEY":
            # Same-shape key — accept the Foundry-named var as a fallback so
            # operators with both surfaces in one .env don't duplicate the key.
            return os.environ.get("AZURE_FOUNDRY_API_KEY")
    return None


def _scan_repo_for_changed_files(repo_path: Path) -> list[str]:
    """Best-effort: ask git for the working-tree change set."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    files: list[str] = []
    for line in result.stdout.splitlines():
        # Porcelain v1: "XY path"
        if len(line) < 4:
            continue
        files.append(line[3:].strip())
    return files


def _run_pytest(repo_path: Path, test_command: str | None = None) -> tuple[bool, str]:
    """Return (passed, captured_output) for the chain's post-sandbox test gate.

    Resolution order:
      1. If ``test_command`` is provided (typically from
         ``app_config.gates.test_command``), run it verbatim via shell. This
         lets monorepo apps — where pytest must run from a sub-directory
         like ``backend/`` rather than the repo root — declare the exact
         invocation themselves.
      2. Otherwise look for ``tests/`` or root-level ``test_*.py`` and run
         ``python -m pytest -q`` from the repo root.
      3. If neither path is viable, return ``(False, "no tests directory")``
         so the caller can record a meaningful signal.
    """
    import subprocess

    if test_command:
        try:
            result = subprocess.run(
                test_command,
                shell=True,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
                timeout=600,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return (False, f"test_command invocation failed: {exc}")
        return (result.returncode == 0, result.stdout + "\n" + result.stderr)

    if not (repo_path / "tests").exists() and not list(repo_path.glob("test_*.py")):
        return (False, "no tests directory")
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "-q"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return (False, f"pytest invocation failed: {exc}")
    return (result.returncode == 0, result.stdout + "\n" + result.stderr)


# --------------------------------------------------------------------------- #
# sandbox_run
# --------------------------------------------------------------------------- #


def _build_initial_message(
    *,
    persona: str,
    story_text: str,
    context_prelude: str,
    persona_prompt: str,
    prior_attempts: list[dict[str, Any]] | None = None,
) -> str:
    parts = [
        context_prelude,
        "---",
        f"# Persona prompt: {persona}",
        persona_prompt.rstrip(),
        "---",
        "# Story",
        story_text.rstrip(),
    ]
    if prior_attempts:
        # The chain feeds prior failed attempts forward so the LLM sees what
        # was already tried and which assertions are still red. Without this,
        # every retry is from scratch (no memory) and re-discovers the same
        # dead ends. Cap each output tail at 1500 chars to keep the prompt
        # bounded — full diagnostic lives in ``state/logs/<story>.log``.
        parts.append("---")
        parts.append("# Previous attempts on THIS story (most recent last)")
        parts.append(
            "These attempts ran in the same git worktree and any file changes "
            "they made persist below. Use them to avoid repeating failed "
            "approaches; if the same test keeps failing for a reason the test "
            "itself is wrong, emit ``TESTS_NEED_CLARIFICATION:`` on a single "
            "line followed by which test and why."
        )
        for entry in prior_attempts:
            parts.append("")
            parts.append(f"## Attempt {entry.get('attempt', '?')}")
            files = entry.get("files_touched") or []
            if files:
                parts.append(f"- Files touched: {', '.join(files[:10])}")
            summary = (entry.get("summary") or "").strip()
            if summary:
                parts.append(f"- Summary: {summary[:400]}")
            tail = (entry.get("test_output_tail") or "").strip()
            if tail:
                parts.append("- Test output tail:")
                parts.append("```")
                parts.append(tail[-1500:])
                parts.append("```")
    return "\n".join(parts) + "\n"


# Per-persona override map for ``sandbox_run.max_iterations``. Personas with
# bounded workflows (Onboarder's 4-phase scan, Test-Implementer's plan
# execution) get tighter caps so the chain doesn't burn budget on an agent
# that lost the plot. ``dev`` keeps the default 200 because it legitimately
# needs many turns for red→green→refactor.
#
# The cap is the *fallback* when the caller passes ``max_iterations=200``
# (the function default). Explicit values from the caller always win — that's
# how tests bound runs and how a power-user can override.
# JSON-mode truncation retry policy. Provider returns finish_reason="length"
# or a partial JSON string when ``max_tokens`` is exceeded. We double the cap
# and retry, up to ``_MAX_OUTPUT_RETRIES`` times, capped at
# ``_MAX_OUTPUT_RETRY_CEILING``. Default seed when callers don't pass
# ``max_tokens`` is conservative — providers bill by actual tokens used so
# the cost cost of generous caps on outputs that fit is zero.
_DEFAULT_MAX_OUTPUT_TOKENS = 8192
_MAX_OUTPUT_RETRIES = 4
_MAX_OUTPUT_RETRY_CEILING = 65536


PERSONA_ITERATION_CAPS: dict[str, int] = {
    # Bumped substantially after D007 showed 60/100 was too tight: onboarder
    # needs to read enough code to write coherent context docs, and
    # test_implementer needs room to write and rewrite tests when its first
    # cut is brittle. Token cost isn't the constraint — sandbox iterations
    # are essentially-free wall-clock until they cap out. Dev keeps the
    # default 600 because it does most of the heavy red→green refactor work.
    "onboarder": 180,
    "test_implementer": 300,
}


async def sandbox_run(
    persona: str,
    story_path: Path,
    repo_path: Path,
    llm_config: LLMConfig,
    difficulty: str = "standard",
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    task_scope: str | None = None,
    max_iterations: int = 600,
    direction_chain: list[Any] | None = None,
    software_factory_root: Path | None = None,
    test_command: str | None = None,
    prior_attempts: list[dict[str, Any]] | None = None,
) -> RunResult:
    """Run a persona inside an OpenHands SDK sandbox against ``repo_path``.

    Reads the persona prompt + story file, composes the context prelude via
    ``factory.context.loader.compose_context_prelude``, hands the combined
    message to a fresh ``Conversation`` with default tools, and waits for
    completion.

    If ``dry_run`` is True, the function does NOT instantiate any SDK objects;
    it assembles the prompt, writes an entry to the DB with ``cost_usd=0`` and
    a synthetic success flag (False — dry-run is a wiring test, not work), and
    returns the assembled prompt as ``RunResult.summary`` so the caller can
    inspect it.
    """
    from factory.context.loader import compose_context_prelude  # local import keeps CLI light

    story_text = Path(story_path).read_text(encoding="utf-8")
    persona_prompt = _read_persona_prompt(persona)
    context_prelude = compose_context_prelude(
        persona=persona,
        app_repo_path=repo_path,
        task_scope=task_scope,
        direction_chain=direction_chain,
        software_factory_root=software_factory_root,
    )
    initial_user_text = _build_initial_message(
        persona=persona,
        story_text=story_text,
        context_prelude=context_prelude,
        persona_prompt=persona_prompt,
        prior_attempts=prior_attempts,
    )

    if dry_run:
        # Walk: did the prelude actually pull in project.md / navigation.md? Surface that.
        # Match the heading form ONLY — the dev persona prompt mentions
        # ``context/project.md`` as a forbidden write path, so a plain substring
        # check would always be a false positive.
        prelude_signals = []
        if "## context/project.md" in context_prelude:
            prelude_signals.append("project.md included")
        if "## context/navigation.md" in context_prelude:
            prelude_signals.append("navigation.md included")
        if "NO CONTEXT AVAILABLE" in context_prelude:
            prelude_signals.append("NO_CONTEXT_AVAILABLE notice issued")

        _record_run(
            persona=persona,
            model=llm_config.model,
            mode="sandbox-dry-run",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            success=False,
            story_path=str(story_path),
            repo_path=str(repo_path),
            error=None,
            db_path=db_path,
        )
        summary = (
            f"[DRY-RUN] persona={persona} model={llm_config.model} difficulty={difficulty}\n"
            f"prelude signals: {', '.join(prelude_signals) or 'none'}\n"
            f"initial_user_text bytes: {len(initial_user_text)}\n"
            f"--- INITIAL USER MESSAGE (head 1200 chars) ---\n"
            f"{initial_user_text[:1200]}\n"
            f"--- END ---"
        )
        return RunResult(
            success=False,
            files_changed=[],
            test_run_passed=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            error=None,
            summary=summary,
        )

    # ---- Real run: instantiate OpenHands SDK Conversation -----------------
    api_key = _resolve_api_key(llm_config)
    if api_key is None:
        err = (
            f"No API key available for model {llm_config.model!r}. Set the appropriate "
            f"env var (DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY / "
            f"AZURE_AI_API_KEY) or pass --dry-run."
        )
        _record_run(
            persona=persona,
            model=llm_config.model,
            mode="sandbox",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            success=False,
            story_path=str(story_path),
            repo_path=str(repo_path),
            error=err,
            db_path=db_path,
        )
        return RunResult(success=False, error=err, summary=err)

    try:
        # Import OpenHands lazily so test/CLI paths that never hit a real run
        # don't pay the import cost. mypy treats OpenHands as untyped via the
        # ignore_missing_imports override; we cast through Any below.
        from openhands.sdk import LLM, Conversation, LocalWorkspace
        from openhands.tools.preset.default import get_default_agent
        from pydantic import SecretStr
    except Exception as exc:  # pragma: no cover - exercised only with SDK
        err = f"OpenHands SDK import failed: {exc}"
        _record_run(
            persona=persona,
            model=llm_config.model,
            mode="sandbox",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            success=False,
            story_path=str(story_path),
            repo_path=str(repo_path),
            error=err,
            db_path=db_path,
        )
        return RunResult(success=False, error=err, summary=err)

    # For Azure (either surface), populate base_url + api_version from env if
    # the caller didn't pass them. The two surfaces read different env vars:
    #
    #   * ``azure_ai/...``  → AZURE_AI_API_BASE  / AZURE_AI_API_VERSION
    #                         (fallback: AZURE_FOUNDRY_ENDPOINT / _API_VERSION)
    #   * ``azure/...``     → AZURE_API_BASE     / AZURE_API_VERSION
    #                         (fallback: AZURE_ENDPOINT, plus the foundry vars
    #                          for operators sharing a single key/endpoint)
    #
    # The LiteLLM monkey-patch in ``factory.providers.azure_foundry.ensure_
    # bootstrapped`` (already called by ``_resolve_api_key`` above) makes the
    # OpenAI-compatible Foundry path work for every ``azure_ai/...`` id.
    base_url = llm_config.base_url
    api_version: str | None = None
    if llm_config.model.startswith("azure_ai/"):
        base_url = (
            base_url
            or os.environ.get("AZURE_AI_API_BASE")
            or os.environ.get("AZURE_FOUNDRY_ENDPOINT")
        )
        api_version = os.environ.get("AZURE_AI_API_VERSION") or os.environ.get(
            "AZURE_FOUNDRY_API_VERSION"
        )
    elif llm_config.model.startswith("azure/"):
        base_url = (
            base_url
            or os.environ.get("AZURE_API_BASE")
            or os.environ.get("AZURE_ENDPOINT")
            or os.environ.get("AZURE_FOUNDRY_ENDPOINT")
        )
        api_version = os.environ.get("AZURE_API_VERSION") or os.environ.get(
            "AZURE_FOUNDRY_API_VERSION"
        )

    llm_kwargs: dict[str, Any] = {
        "model": llm_config.model,
        "api_key": SecretStr(api_key),
        "base_url": base_url,
        "usage_id": f"factory:{persona}",
    }
    if api_version is not None:
        llm_kwargs["api_version"] = api_version
    llm = LLM(**llm_kwargs)
    agent = get_default_agent(llm=llm, cli_mode=True)
    workspace = LocalWorkspace(working_dir=str(Path(repo_path).resolve()))

    # Apply per-persona iteration cap when the caller used the default. We
    # detect "default" by comparing to the signature default (200). Callers
    # who explicitly pass a non-default value win; this only narrows the
    # ceiling for personas that historically over-iterate.
    # Apply per-persona iteration cap when the caller used the signature
    # default (we read the live default off the function signature so this
    # detection survives future bumps to the default without code churn).
    import inspect as _inspect

    _signature_default = _inspect.signature(sandbox_run).parameters["max_iterations"].default
    effective_max_iterations = max_iterations
    if max_iterations == _signature_default and persona in PERSONA_ITERATION_CAPS:
        effective_max_iterations = PERSONA_ITERATION_CAPS[persona]

    loop = asyncio.get_running_loop()

    def _do_run() -> tuple[int, int, float]:
        # ``Conversation`` is a factory that returns LocalConversation/RemoteConversation
        # depending on the workspace type. Treat as Any for mypy purposes.
        conversation: Any = Conversation(
            agent=agent,
            workspace=workspace,
            max_iteration_per_run=effective_max_iterations,
            delete_on_close=False,
        )
        try:
            conversation.send_message(initial_user_text)
            conversation.run()
            stats = conversation.conversation_stats.get_combined_metrics()
            tok = stats.accumulated_token_usage
            t_in = int(getattr(tok, "prompt_tokens", 0) or 0)
            t_out = int(getattr(tok, "completion_tokens", 0) or 0)
            cost = float(getattr(stats, "accumulated_cost", 0.0) or 0.0)
            return (t_in, t_out, cost)
        finally:
            conversation.close()

    try:
        tokens_in, tokens_out, cost_usd = await loop.run_in_executor(None, _do_run)
    except Exception as exc:
        err = f"sandbox run raised: {exc!r}"
        _record_run(
            persona=persona,
            model=llm_config.model,
            mode="sandbox",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            success=False,
            story_path=str(story_path),
            repo_path=str(repo_path),
            error=err,
            db_path=db_path,
        )
        return RunResult(success=False, error=err, summary=err)

    files_changed = _scan_repo_for_changed_files(Path(repo_path))
    test_passed, test_out = _run_pytest(Path(repo_path), test_command=test_command)

    _record_run(
        persona=persona,
        model=llm_config.model,
        mode="sandbox",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        success=test_passed,
        story_path=str(story_path),
        repo_path=str(repo_path),
        error=None if test_passed else "tests not green after run",
        db_path=db_path,
    )

    return RunResult(
        success=test_passed,
        files_changed=files_changed,
        test_run_passed=test_passed,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        error=None if test_passed else "tests not green after run",
        summary=test_out[-2000:],
    )


# --------------------------------------------------------------------------- #
# text_run
# --------------------------------------------------------------------------- #


def text_run(
    persona: str,
    prompt: str,
    model_id: str,
    schema: dict[str, Any] | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    db_path: Path | None = None,
    dry_run: bool = False,
    max_tokens: int | None = None,
) -> str | dict[str, Any]:
    """Single ``litellm.completion()`` call. Returns text, or a dict if ``schema`` set.

    When ``schema`` is provided, the prompt is augmented with an instruction
    to return JSON matching the schema; the response is parsed and validated
    via ``jsonschema`` if installed, falling back to a minimal key-presence
    check otherwise.
    """
    cfg = LLMConfig(model=model_id, api_key=api_key, base_url=base_url)
    resolved_key = _resolve_api_key(cfg)

    if dry_run:
        _record_run(
            persona=persona,
            model=model_id,
            mode="text-dry-run",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            success=True,
            story_path=None,
            repo_path=None,
            error=None,
            db_path=db_path,
        )
        if schema is not None:
            return {"_dry_run": True, "persona": persona, "model": model_id}
        return f"[DRY-RUN text_run] persona={persona} model={model_id}"

    if resolved_key is None:
        msg = f"No API key available for {model_id!r}"
        _record_run(
            persona=persona,
            model=model_id,
            mode="text",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            success=False,
            story_path=None,
            repo_path=None,
            error=msg,
            db_path=db_path,
        )
        raise RuntimeError(msg)

    try:
        import litellm
    except Exception as exc:
        raise RuntimeError(f"litellm not installed: {exc}") from exc

    # Azure (either surface): ensure base_url is present so LiteLLM hits the
    # right URL shape. Env-var precedence differs by surface — see
    # ``_provider_env_key`` for the rationale.
    effective_base_url = base_url
    api_version: str | None = None
    if model_id.startswith("azure_ai/"):
        if effective_base_url is None:
            effective_base_url = os.environ.get("AZURE_AI_API_BASE") or os.environ.get(
                "AZURE_FOUNDRY_ENDPOINT"
            )
        api_version = os.environ.get("AZURE_AI_API_VERSION") or os.environ.get(
            "AZURE_FOUNDRY_API_VERSION"
        )
    elif model_id.startswith("azure/"):
        if effective_base_url is None:
            effective_base_url = (
                os.environ.get("AZURE_API_BASE")
                or os.environ.get("AZURE_ENDPOINT")
                or os.environ.get("AZURE_FOUNDRY_ENDPOINT")
            )
        api_version = os.environ.get("AZURE_API_VERSION") or os.environ.get(
            "AZURE_FOUNDRY_API_VERSION"
        )

    messages = [{"role": "user", "content": prompt}]
    if schema is not None:
        messages[0]["content"] = (
            f"{prompt}\n\nReturn ONLY a JSON object matching this schema:\n"
            f"{json.dumps(schema, indent=2)}"
        )

    # Retry loop for JSON-mode truncation: when finish_reason == "length"
    # OR the response fails to parse, double max_tokens and retry. Hard
    # ceiling _MAX_OUTPUT_RETRY_CEILING covers every model in our fleet
    # (Claude 4.x supports 32k; Azure GPT 5.4 supports 16k+; DeepSeek
    # caps at 8k and will silently clip past that, which is fine because
    # we'll surface the parse error rather than loop forever).
    #
    # Truncation is *visible* in two places: ``finish_reason="length"``
    # from the provider, and a json.loads exception on the partial text.
    # Either signal triggers the retry; we keep doubling up to the
    # ceiling so a single 4096-cap mistake doesn't wedge the chain.
    current_max = max_tokens if max_tokens is not None else _DEFAULT_MAX_OUTPUT_TOKENS
    tokens_in = 0
    tokens_out = 0
    cost_usd = 0.0
    text = ""
    parsed: dict[str, Any] | None = None
    last_finish_reason: str | None = None

    for attempt in range(1, _MAX_OUTPUT_RETRIES + 1):
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "api_key": resolved_key,
        }
        if effective_base_url:
            kwargs["base_url"] = effective_base_url
        if api_version:
            kwargs["api_version"] = api_version
        kwargs["max_tokens"] = current_max
        if schema is not None:
            kwargs["response_format"] = {"type": "json_object"}

        response = litellm.completion(**kwargs)
        text = response["choices"][0]["message"]["content"]
        usage = response.get("usage", {}) or {}
        tokens_in += int(usage.get("prompt_tokens", 0) or 0)
        tokens_out += int(usage.get("completion_tokens", 0) or 0)
        try:
            cost_usd += float(getattr(response, "_hidden_params", {}).get("response_cost") or 0.0)
        except Exception:
            pass
        try:
            last_finish_reason = response["choices"][0].get("finish_reason")
        except Exception:
            last_finish_reason = None

        if schema is None:
            # Plain text mode — only retry on explicit truncation.
            if last_finish_reason != "length" or current_max >= _MAX_OUTPUT_RETRY_CEILING:
                break
        else:
            try:
                parsed = json.loads(text)
                break
            except Exception as parse_exc:
                if current_max >= _MAX_OUTPUT_RETRY_CEILING or attempt == _MAX_OUTPUT_RETRIES:
                    # No more headroom — record and raise with full diagnostics.
                    _record_run(
                        persona=persona,
                        model=model_id,
                        mode="text",
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        cost_usd=cost_usd,
                        success=False,
                        story_path=None,
                        repo_path=None,
                        error=(
                            f"json parse failed at max_tokens={current_max} "
                            f"finish_reason={last_finish_reason}: {parse_exc}"
                        ),
                        db_path=db_path,
                    )
                    raise RuntimeError(
                        f"JSON-mode response was not valid JSON after "
                        f"{attempt} attempts (max_tokens up to {current_max}, "
                        f"finish_reason={last_finish_reason}): {parse_exc}"
                    ) from parse_exc

        # Double for next attempt; clamp to ceiling.
        current_max = min(current_max * 2, _MAX_OUTPUT_RETRY_CEILING)

    success = True

    _record_run(
        persona=persona,
        model=model_id,
        mode="text",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        success=success,
        story_path=None,
        repo_path=None,
        error=None,
        db_path=db_path,
    )

    if schema is not None:
        return cast(dict[str, Any], parsed)
    return cast(str, text)
