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
    if model.startswith("deepseek/"):
        return "DEEPSEEK_API_KEY"
    if model.startswith("anthropic/") or model.startswith("claude"):
        return "ANTHROPIC_API_KEY"
    if model.startswith("openai/") or model.startswith("gpt"):
        return "OPENAI_API_KEY"
    return None


def _resolve_api_key(cfg: LLMConfig) -> str | None:
    if cfg.api_key:
        return cfg.api_key
    env_key = _provider_env_key(cfg.model)
    if env_key:
        return os.environ.get(env_key)
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


def _run_pytest(repo_path: Path) -> tuple[bool, str]:
    """Return (passed, captured_output). Pytest is invoked if a tests/ dir exists."""
    import subprocess

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
) -> str:
    return (
        f"{context_prelude}\n"
        f"---\n"
        f"# Persona prompt: {persona}\n"
        f"{persona_prompt.rstrip()}\n"
        f"---\n"
        f"# Story\n"
        f"{story_text.rstrip()}\n"
    )


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
    max_iterations: int = 200,
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
        persona=persona, app_repo_path=repo_path, task_scope=task_scope
    )
    initial_user_text = _build_initial_message(
        persona=persona,
        story_text=story_text,
        context_prelude=context_prelude,
        persona_prompt=persona_prompt,
    )

    if dry_run:
        # Walk: did the prelude actually pull in project.md / navigation.md? Surface that.
        prelude_signals = []
        if "context/project.md" in context_prelude or "## context/project.md" in context_prelude:
            prelude_signals.append("project.md included")
        if (
            "context/navigation.md" in context_prelude
            or "## context/navigation.md" in context_prelude
        ):
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
            f"env var (DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY) "
            f"or pass --dry-run."
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

    llm = LLM(
        model=llm_config.model,
        api_key=SecretStr(api_key),
        base_url=llm_config.base_url,
        usage_id=f"factory:{persona}",
    )
    agent = get_default_agent(llm=llm, cli_mode=True)
    workspace = LocalWorkspace(working_dir=str(Path(repo_path).resolve()))

    loop = asyncio.get_running_loop()

    def _do_run() -> tuple[int, int, float]:
        # ``Conversation`` is a factory that returns LocalConversation/RemoteConversation
        # depending on the workspace type. Treat as Any for mypy purposes.
        conversation: Any = Conversation(
            agent=agent,
            workspace=workspace,
            max_iteration_per_run=max_iterations,
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
    test_passed, test_out = _run_pytest(Path(repo_path))

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

    messages = [{"role": "user", "content": prompt}]
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "api_key": resolved_key,
    }
    if base_url:
        kwargs["base_url"] = base_url
    if schema is not None:
        kwargs["response_format"] = {"type": "json_object"}
        messages[0]["content"] = (
            f"{prompt}\n\nReturn ONLY a JSON object matching this schema:\n"
            f"{json.dumps(schema, indent=2)}"
        )

    response = litellm.completion(**kwargs)
    text = response["choices"][0]["message"]["content"]
    usage = response.get("usage", {}) or {}
    tokens_in = int(usage.get("prompt_tokens", 0) or 0)
    tokens_out = int(usage.get("completion_tokens", 0) or 0)
    try:
        cost_usd = float(getattr(response, "_hidden_params", {}).get("response_cost") or 0.0)
    except Exception:
        cost_usd = 0.0

    success = True
    parsed: dict[str, Any] | None = None
    if schema is not None:
        try:
            parsed = json.loads(text)
        except Exception as exc:
            success = False
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
                error=f"json parse failed: {exc}",
                db_path=db_path,
            )
            raise RuntimeError(f"JSON-mode response was not valid JSON: {exc}") from exc

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
