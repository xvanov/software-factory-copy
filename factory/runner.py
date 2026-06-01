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
import contextlib
import json
import os
import time
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

# Wall-clock ceiling for a single sandbox conversation run. The SDK's
# ``max_iteration`` bounds the number of tool calls but NOT a stalled LLM call,
# so a hung request (network stall, provider deadlock) could block a handler
# indefinitely — one dev tick was observed stuck for 51 minutes at ~0% CPU. A
# normal sandbox run finishes in 1-15 min; this ceiling is generous enough not
# to false-kill legitimate long runs (e.g. a ~15 min test_implementer) while
# still reaping a true hang. On timeout the run returns the same infra-retryable
# shape as any other pre-model failure (success=False, test_run_passed=None,
# zero cost), so handle_dev's infra circuit breaker re-dispatches without
# burning the retry budget. Override via FACTORY_SANDBOX_TIMEOUT_S.
_SANDBOX_WALL_CLOCK_TIMEOUT_S = int(os.environ.get("FACTORY_SANDBOX_TIMEOUT_S", "1800"))


# Markers that indicate a persona prompt was assembled with literal
# placeholders instead of real fetched data. Kept in sync with
# ``factory.chain.handlers._BROKEN_PROMPT_MARKERS`` — duplicated here so
# the logger has no chain->runner dependency. New markers should be added
# in BOTH places, and ideally added with a corresponding contract test.
_BROKEN_PROMPT_MARKERS: tuple[str, ...] = (
    "(fetched from GitHub by the chain",
    "placeholder for real-run",
    "(see {",
)


def _summarize_prompt_sections(prompt: str) -> dict[str, int]:
    """Return ``{section_header: char_count}`` for ``## `` headed sections.

    Lightweight markdown-style parser: every line starting with ``"## "`` is
    a section start; content until the next ``"## "`` (or end-of-string) is
    that section's body. Header lines themselves are excluded from the count.
    """
    sections: dict[str, int] = {}
    current_header: str | None = None
    current_chars = 0
    for line in prompt.splitlines():
        if line.startswith("## "):
            if current_header is not None:
                sections[current_header] = sections.get(current_header, 0) + current_chars
            current_header = line[3:].strip() or "(unnamed)"
            current_chars = 0
        elif current_header is not None:
            current_chars += len(line) + 1  # +1 for the newline
    if current_header is not None:
        sections[current_header] = sections.get(current_header, 0) + current_chars
    return sections


def _log_prompt_metadata(
    *,
    persona: str,
    prompt: str,
    model_id: str,
    story_id: int | None,
    software_factory_root: Path | None,
) -> None:
    """Best-effort: append one record to ``state/events/prompts.ndjson``.

    Records ONLY metadata (lengths, section header names, placeholder
    markers found, sha256 prefix) — never the prompt content itself.
    A failure here MUST NOT break the LLM call.
    """
    try:
        import hashlib

        from factory.manager.signals import write_event

        # The marker scan catches CHAIN personas (dev/review/tech_writer)
        # shipping literal placeholders in place of real fetched data. It does
        # NOT apply to the FMS's own manager_* personas: their prompts echo the
        # placeholder_prompts detector's flagged rows — marker strings and all —
        # back as analysis input, so scanning them produces guaranteed false
        # positives that the detector then re-escalates in a self-sustaining
        # loop. Never stamp markers on a manager_* prompt at the source.
        if persona.startswith("manager_"):
            markers_found: list[str] = []
        else:
            markers_found = [m for m in _BROKEN_PROMPT_MARKERS if m in prompt]
        section_lengths = _summarize_prompt_sections(prompt)
        digest = hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()[:16]
        write_event(
            "prompts",
            {
                "event": "prompt",
                "persona": persona,
                "story_id": story_id,
                "model_id": model_id,
                "prompt_length_total": len(prompt),
                "prompt_section_lengths": section_lengths,
                "placeholder_markers_found": markers_found,
                "prompt_hash": digest,
            },
            software_factory_root=software_factory_root,
        )
    except Exception:  # noqa: BLE001 — logging must never break the call
        pass


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
    # Richer signal extracted from the sandbox conversation for cross-retry
    # memory. ``last_assistant_message`` is the verbatim final assistant
    # message (capped); ``recent_tool_calls`` is the trailing window of
    # (tool, args_excerpt, observation_excerpt) so the next retry can see
    # *what dev was doing* when it gave up — not just the test-output tail.
    # ``self_summary`` is dev's own 3-5 sentence reflection (parsed from
    # the ``SELF_SUMMARY:`` marker the dev persona prompt requests). All
    # three fall back to empty / [] when the conversation didn't expose
    # them; callers must tolerate the empty case.
    last_assistant_message: str = ""
    recent_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    self_summary: str = ""


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
    # Observability / EBS instrumentation. ``duration_s`` is the wall-clock
    # time the runner spent inside the LLM call (set by sandbox_run /
    # text_run on exit). ``story_id`` lets the TUI tie a run back to its
    # story for per-direction progress / velocity sampling. ``model_tier``
    # is the route's difficulty bucket (standard/hard) when available.
    duration_s: float | None = None
    story_id: int | None = None
    model_tier: str | None = None


def _engine(db_path: Path | None = None) -> Any:
    path = db_path or _DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # Run idempotent schema migrations so old DBs gain ``duration_s`` /
    # ``story_id`` / ``model_tier`` on ``runs`` and ``points`` /
    # ``estimated_seconds`` on ``stories`` without dropping data.
    from factory.observability.schema import migrate

    migrate(path)
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
    duration_s: float | None = None,
    story_id: int | None = None,
    model_tier: str | None = None,
    software_factory_root: Path | None = None,
    started_at: str | None = None,
) -> None:
    ended_at = datetime.now(UTC).isoformat()
    engine = _engine(db_path)
    with Session(engine) as session:
        row = Run(
            ts=ended_at,
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
            duration_s=duration_s,
            story_id=story_id,
            model_tier=model_tier,
        )
        session.add(row)
        session.commit()

        # Count prior runs with the same story_id + persona to derive attempt_n.
        # Do this inside the same session so the row we just committed is counted.
        try:
            from sqlmodel import select as _select

            attempt_n = (
                session.exec(
                    _select(Run).where(
                        Run.persona == persona,
                        Run.story_id == story_id,
                    )
                )
                .all()
                .__len__()
            )
        except Exception:
            attempt_n = 1

    # Emit the structured signal — best-effort, never raises.
    try:
        from factory.manager.signals import write_run_event

        _root = software_factory_root or (
            Path(db_path).parent.parent if db_path is not None else None
        )
        write_run_event(
            started_at=started_at or ended_at,
            ended_at=ended_at,
            duration_s=duration_s,
            cost_usd=cost_usd,
            success=success,
            error=error,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            model_tier=model_tier,
            attempt_n=attempt_n,
            story_id=story_id,
            persona=persona,
            worktree_path=repo_path,
            software_factory_root=_root,
        )
    except Exception:  # noqa: BLE001
        pass


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


# Cap for extracted strings — kept in module scope so tests can assert
# against the limits. ``RECENT_TOOL_CALL_WINDOW`` is the number of trailing
# action/observation pairs we keep; each gets its args + observation
# truncated to ``_TOOL_FIELD_CHAR_CAP`` so the JSON we persist on the
# story stays a few KB, not a few MB.
RECENT_TOOL_CALL_WINDOW = 8
_TOOL_FIELD_CHAR_CAP = 600
_LAST_MSG_CHAR_CAP = 2000

# Marker the dev persona emits for its 3-5-sentence self-summary at the end
# of a run. Falls back to the trailing assistant message if absent.
_SELF_SUMMARY_MARKER = "SELF_SUMMARY:"


def _extract_self_summary(last_assistant_message: str) -> str:
    """Pull the ``SELF_SUMMARY:`` paragraph out of the last assistant message.

    The dev persona prompt asks for ``SELF_SUMMARY: <3-5 sentences>``
    before exit. If the marker is present, we return the text following
    it (up to the next blank line or end-of-message). If not, we fall
    back to the trailing 500 chars of the message — better than nothing
    so the next retry has *some* free-form context to read.
    """
    if not last_assistant_message:
        return ""
    idx = last_assistant_message.find(_SELF_SUMMARY_MARKER)
    if idx == -1:
        return last_assistant_message[-500:].strip()
    tail = last_assistant_message[idx + len(_SELF_SUMMARY_MARKER) :].lstrip()
    # Stop at a blank-line boundary so we don't pull in a wall of trailing
    # tool logs the persona may have appended.
    blank = tail.find("\n\n")
    return (tail[:blank] if blank != -1 else tail).strip()[:_LAST_MSG_CHAR_CAP]


def _extract_conversation_memory(conversation: Any) -> tuple[str, list[dict[str, Any]]]:
    """Pull cross-retry memory signal from an OpenHands ``Conversation``.

    Returns ``(last_assistant_message, recent_tool_calls)``. Each tool
    call dict has the shape::

        {
          "tool": "<name>",          # e.g. "execute_bash", "str_replace_editor"
          "args": "<truncated>",     # JSON-ish excerpt of the call args
          "observation": "<truncated>",  # truncated tool output
        }

    Robust to SDK shape changes — every attribute access is defensive,
    every coercion goes through ``str()`` with a fallback. A failure here
    must not break the run; we return ``("", [])``.
    """
    last_msg = ""
    pairs: list[dict[str, Any]] = []
    try:
        state = getattr(conversation, "state", None)
        if state is None:
            return last_msg, pairs
        events = list(getattr(state, "events", []) or [])
    except Exception:
        return last_msg, pairs

    # Walk events in order. Build (action, observation) pairs by matching
    # tool_call_id when the SDK exposes one; otherwise pair consecutive
    # action+observation events in the stream.
    actions_by_id: dict[str, dict[str, Any]] = {}
    ordered_pairs: list[dict[str, Any]] = []
    for ev in events:
        kind = (getattr(ev, "kind", None) or type(ev).__name__).lower()
        # Capture assistant messages — last one wins.
        if "message" in kind:
            source = getattr(ev, "source", "") or ""
            role = getattr(ev, "role", "") or source
            if str(role).lower() in {"assistant", "agent"}:
                content = _stringify_message_content(ev)
                if content:
                    last_msg = content
        # Tool actions.
        if "action" in kind and "agent" not in kind and "rejection" not in kind:
            tool_name = (
                getattr(ev, "tool_name", None)
                or getattr(ev, "name", None)
                or getattr(getattr(ev, "action", None), "tool", None)
                or "tool"
            )
            args_excerpt = _safe_truncate(_stringify_action_args(ev), _TOOL_FIELD_CHAR_CAP)
            tcid = getattr(ev, "tool_call_id", None)
            record = {"tool": str(tool_name), "args": args_excerpt, "observation": ""}
            if tcid is not None:
                actions_by_id[str(tcid)] = record
            ordered_pairs.append(record)
            continue
        # Observations — attach to the matching action by id, or to the
        # most-recent action in stream order if no id match.
        if "observation" in kind:
            obs_text = _safe_truncate(_stringify_observation(ev), _TOOL_FIELD_CHAR_CAP)
            tcid = getattr(ev, "tool_call_id", None)
            if tcid is not None and str(tcid) in actions_by_id:
                actions_by_id[str(tcid)]["observation"] = obs_text
            elif ordered_pairs:
                ordered_pairs[-1]["observation"] = obs_text

    # Keep just the trailing window so the persisted JSON stays bounded.
    pairs = ordered_pairs[-RECENT_TOOL_CALL_WINDOW:]
    last_msg = (last_msg or "")[-_LAST_MSG_CHAR_CAP:]
    return last_msg, pairs


def _stringify_message_content(ev: Any) -> str:
    """Best-effort extract of a message event's text content."""
    # Common shape: ev.llm_message.content is a list of dicts {type, text}
    # OR ev.message.content is a similar list. The SDK has shifted naming
    # across versions; try a few attribute paths.
    for attr in ("llm_message", "message"):
        msg = getattr(ev, attr, None)
        if msg is None:
            continue
        content = getattr(msg, "content", None)
        if content is None:
            continue
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for c in content:
                if isinstance(c, dict):
                    text = c.get("text") or c.get("content") or ""
                    parts.append(str(text))
                else:
                    text = getattr(c, "text", None) or str(c)
                    parts.append(text)
            joined = "\n".join(p for p in parts if p)
            if joined:
                return joined
    # Fallback: model_dump() and pull a "content" field.
    try:
        data = ev.model_dump()  # type: ignore[attr-defined]
    except Exception:
        return ""
    for path in (("llm_message", "content"), ("message", "content"), ("content",)):
        node: Any = data
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return "\n".join(str(c.get("text", c)) if isinstance(c, dict) else str(c) for c in node)
    return ""


def _stringify_action_args(ev: Any) -> str:
    """Pull a JSON-ish representation of a tool call's args off the event."""
    for attr in ("arguments", "args", "tool_args", "action"):
        val = getattr(ev, attr, None)
        if val is None:
            continue
        try:
            return json.dumps(val, default=str)
        except Exception:
            return str(val)
    try:
        data = ev.model_dump()  # type: ignore[attr-defined]
        return json.dumps(data, default=str)
    except Exception:
        return ""


def _stringify_observation(ev: Any) -> str:
    """Pull the text body of a tool observation event."""
    for attr in ("output", "content", "result", "text", "observation"):
        val = getattr(ev, attr, None)
        if isinstance(val, str) and val:
            return val
        if val is None:
            continue
        try:
            return json.dumps(val, default=str)
        except Exception:
            return str(val)
    try:
        data = ev.model_dump()  # type: ignore[attr-defined]
        return json.dumps(data, default=str)
    except Exception:
        return ""


def _safe_truncate(text: str, cap: int) -> str:
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= cap:
        return text
    return text[: cap - 20] + "...[truncated]"


def _isolated_test_env() -> dict[str, str]:
    """Environment for a test-gate subprocess, isolated per run.

    Two harness-level fixes for last-mile false failures observed in the
    sacrifice queue:

      * ``MEDIA_DIR`` → a fresh writable tmp dir. The app's default
        ``media_dir`` is ``/var/sacrifice/media`` (not writable in the
        sandbox), so any story whose code does ``mkdir(parents=True)`` under
        it fails the gate with ``PermissionError`` — a harness/env defect, not
        a code defect (observed: story 18's upload smoke test). A per-run tmp
        dir also keeps concurrent story gates from colliding on shared media
        state.
      * ``PYTHONDONTWRITEBYTECODE`` → stop leaving ``__pycache__`` behind, so a
        reused worktree can't collect a sibling story's stale ``.pyc`` and run
        a test that isn't on this branch (observed: story 20).
    """
    import tempfile

    env = dict(os.environ)
    env["MEDIA_DIR"] = tempfile.mkdtemp(prefix="factory-test-media-")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


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

    Runs under ``_isolated_test_env`` so a non-writable ``media_dir`` default
    or stale bytecode can't manufacture a false failure.
    """
    import subprocess

    test_env = _isolated_test_env()

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
                env=test_env,
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
            env=test_env,
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
    reviewer_findings: dict[str, Any] | None = None,
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
    # When the chain bounces a story back from the reviewer (state
    # REVIEWER_REQUESTED_CHANGES -> dev), the tests are already green, so the
    # dev LLM has no signal about WHY it was re-dispatched unless we hand it
    # the reviewer's actual change requests. Without this section dev fixes
    # blind, the reviewer re-raises the same findings, and the loop never
    # converges. Render the findings prominently, right after the story.
    if reviewer_findings:
        findings = reviewer_findings.get("findings") or []
        tq_findings = reviewer_findings.get("test_quality_findings") or []
        summary = (reviewer_findings.get("summary") or "").strip()
        # Loop-4: the dev persona owns BOTH code and tests, so its branch frames
        # every finding (code AND test-quality) as dev's to fix. The
        # test_implementer/test_designer branch is retained for any legacy use
        # but those personas are no longer dispatched by the chain.
        is_test_persona = persona in ("test_implementer", "test_designer")
        if findings or tq_findings or summary:
            parts.append("---")
            if is_test_persona:
                parts.append("# Reviewer rejected the TESTS — rewrite them to fix this")
                parts.append(
                    "The reviewer rejected the previous revision on test "
                    "quality. Rewrite the test files so they resolve EVERY "
                    "concern below — correct file location per the test plan, "
                    "tighten weak/sloppy assertions, and add the missing "
                    "behavioral coverage. Editing the test files is exactly "
                    "your job here; do not touch production code."
                )
                if summary:
                    parts.append(f"\n**Reviewer summary:** {summary[:600]}")
                if tq_findings:
                    parts.append("\n## Test-quality findings (fix each)")
                for j, f in enumerate(tq_findings, 1):
                    name = f.get("test_name", "")
                    issue = (f.get("issue") or "").strip()
                    fix = (f.get("fix_suggestion") or "").strip()
                    parts.append(f"\n{j}. `{name}`".rstrip())
                    if issue:
                        parts.append(f"   - Issue: {issue[:400]}")
                    if fix:
                        parts.append(f"   - Suggested fix: {fix[:400]}")
                # Code findings that are really about tests/coverage also help.
                if findings:
                    parts.append("\n## Reviewer code/coverage findings (for context)")
                for i, f in enumerate(findings, 1):
                    loc = f.get("location", "")
                    what = (f.get("what") or "").strip()
                    parts.append(f"\n{i}. {loc}".rstrip())
                    if what:
                        parts.append(f"   - {what[:400]}")
            else:
                parts.append("# Reviewer change requests — you MUST address ALL of these")
                parts.append(
                    "The reviewer rejected the previous revision of this PR. You "
                    "own BOTH the production code AND the tests, so resolve EVERY "
                    "item below: fix the code findings in the source, AND fix the "
                    "test-quality findings by editing the tests themselves. Then "
                    "re-run the full suite — it must stay green. If a request is "
                    "genuinely wrong or impossible, say so explicitly in your "
                    "summary rather than silently ignoring it — leaving any item "
                    "unaddressed will cause the reviewer to reject again."
                )
                if summary:
                    parts.append(f"\n**Reviewer summary:** {summary[:600]}")
                if findings:
                    parts.append("\n## Code change requests (fix these in production code)")
                for i, f in enumerate(findings, 1):
                    sev = f.get("severity", "?")
                    loc = f.get("location", "")
                    what = (f.get("what") or "").strip()
                    fix = (f.get("fix_suggestion") or "").strip()
                    parts.append(f"\n{i}. **[{sev}]** {loc}".rstrip())
                    if what:
                        parts.append(f"   - Problem: {what[:500]}")
                    if fix:
                        parts.append(f"   - Suggested fix: {fix[:500]}")
                # Test-quality findings: dev OWNS the tests now, so fix them
                # directly — make each test exercise the real behavior and assert
                # the correct contract value the reviewer flagged.
                if tq_findings:
                    parts.append("\n## Test-quality findings (fix these tests directly)")
                    parts.append(
                        "The reviewer flagged the tests below as weak or wrong. "
                        "Edit each test so it drives the REAL behavior end-to-end "
                        "and asserts the correct contract value — do not delete or "
                        "weaken them to dodge the finding."
                    )
                    for j, f in enumerate(tq_findings, 1):
                        name = f.get("test_name", "")
                        issue = (f.get("issue") or "").strip()
                        fix = (f.get("fix_suggestion") or "").strip()
                        parts.append(f"\nTest-quality {j}. `{name}`".rstrip())
                        if issue:
                            parts.append(f"   - Issue: {issue[:400]}")
                        if fix:
                            parts.append(f"   - Suggested fix: {fix[:400]}")
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
            "approaches; if the same test keeps failing because the test itself "
            "is wrong, fix the test (you own it) — make it assert the correct "
            "behavior rather than working around a bad assertion."
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

        # Cross-retry memory: dev's own reasoning + the trailing tool-call
        # window. Captured by ``_extract_conversation_memory`` after each
        # sandbox closes. The next retry sees not just *what failed* but
        # *what dev was trying to do* — the difference between giving the
        # LLM a stack trace and giving it the previous LLM's notebook.
        prior_thinking_entries = [
            e
            for e in prior_attempts
            if (
                e.get("self_summary")
                or e.get("last_assistant_message")
                or e.get("recent_tool_calls")
            )
        ]
        if prior_thinking_entries:
            parts.append("")
            parts.append("---")
            parts.append("# Your prior thinking (from previous sandbox sessions)")
            parts.append(
                "Each retry runs a fresh OpenHands conversation, but the "
                "previous run's last assistant message, recent tool calls, "
                "and self-summary are surfaced here so you keep context "
                "across the retry boundary. Do NOT repeat the exact "
                "approach if it failed; use this to inform a new line of "
                "investigation."
            )
            for entry in prior_thinking_entries:
                parts.append("")
                parts.append(f"## Attempt {entry.get('attempt', '?')} — prior thinking")
                self_sum = (entry.get("self_summary") or "").strip()
                if self_sum:
                    parts.append(
                        "### Self-summary (what I tried / what failed / what I'd try next)"
                    )
                    parts.append(self_sum[:1500])
                last_msg = (entry.get("last_assistant_message") or "").strip()
                if last_msg and last_msg != self_sum:
                    parts.append("### Last assistant message (verbatim tail)")
                    parts.append("```")
                    parts.append(last_msg[-1200:])
                    parts.append("```")
                calls = entry.get("recent_tool_calls") or []
                if calls:
                    parts.append("### Recent tool calls (trailing window)")
                    for i, call in enumerate(calls[-RECENT_TOOL_CALL_WINDOW:], 1):
                        tool = call.get("tool", "tool")
                        args = (call.get("args") or "")[:300]
                        obs = (call.get("observation") or "")[:300]
                        parts.append(f"{i}. **{tool}** — args: `{args}`")
                        if obs:
                            parts.append(f"   → `{obs}`")
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
    reviewer_findings: dict[str, Any] | None = None,
    story_id: int | None = None,
    app: str | None = None,
    direction_id: str | None = None,
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
        reviewer_findings=reviewer_findings,
    )

    _t0 = time.monotonic()
    _started_at = datetime.now(UTC).isoformat()

    def _elapsed() -> float:
        return round(time.monotonic() - _t0, 3)

    def _record(**kw: Any) -> None:
        """Thin wrapper that injects started_at + software_factory_root."""
        _record_run(
            **kw,
            started_at=_started_at,
            software_factory_root=software_factory_root,
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

        _record(
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
            duration_s=_elapsed(),
            story_id=story_id,
            model_tier=difficulty,
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
        _record(
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
            duration_s=_elapsed(),
            story_id=story_id,
            model_tier=difficulty,
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
        _record(
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
            duration_s=_elapsed(),
            story_id=story_id,
            model_tier=difficulty,
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

    # Give the dev/test_impl sandbox a writable MEDIA_DIR so the agent's OWN
    # in-loop test runs don't fail on the unwritable ``/var/sacrifice`` default
    # (which would make dev "see red" on correct code and thrash). The post-
    # sandbox gate (_run_pytest) still uses its own fresh tmp via
    # _isolated_test_env; this only affects what the agent observes mid-run.
    # Process-global is fine: each ``factory tick`` is its own process running
    # handlers serially. PYTHONDONTWRITEBYTECODE avoids stale .pyc accumulation.
    import tempfile as _tf

    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    os.environ["MEDIA_DIR"] = _tf.mkdtemp(prefix="factory-sandbox-media-")

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

    def _do_run() -> tuple[int, int, float, str, list[dict[str, Any]]]:
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
            # Extract cross-retry memory signal from the conversation's
            # event stream BEFORE closing. ``conversation.state.events`` is
            # the canonical sequence of MessageEvent / ActionEvent /
            # ObservationEvent records. We do the extraction inside the
            # executor (same thread that owns the state) and pass plain
            # dicts back to the async layer.
            last_msg, recent = _extract_conversation_memory(conversation)
            return (t_in, t_out, cost, last_msg, recent)
        finally:
            conversation.close()

    # Write a ``live_handlers`` heartbeat row so the TUI can see what's
    # mid-flight. The context manager removes the row on exit regardless
    # of success/failure; reaped on stale-pid scan if the process crashes.
    from factory.observability.heartbeat import live_handler

    _hb_db = db_path or _DEFAULT_DB_PATH
    try:
        with live_handler(
            _hb_db,
            persona=persona,
            model=llm_config.model,
            mode="sandbox",
            story_id=story_id,
            app=app,
            direction_id=direction_id,
        ):
            (
                tokens_in,
                tokens_out,
                cost_usd,
                last_assistant_message,
                recent_tool_calls,
                # Bound the blocking executor call so a stalled LLM request can't
                # hang the handler forever. asyncio.wait_for cancels the await on
                # timeout; the orphaned worker thread (threads can't be force-
                # killed in-process) is reaped when this one-shot tick process
                # exits. The TimeoutError is handled distinctly below.
            ) = await asyncio.wait_for(
                loop.run_in_executor(None, _do_run),
                timeout=_SANDBOX_WALL_CLOCK_TIMEOUT_S,
            )
    except TimeoutError:
        err = (
            f"sandbox run timed out after {_SANDBOX_WALL_CLOCK_TIMEOUT_S}s "
            "(likely a stalled LLM call); treating as retryable infrastructure "
            "failure"
        )
        _record(
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
            duration_s=_elapsed(),
            story_id=story_id,
            model_tier=difficulty,
        )
        # test_run_passed defaults to None + zero cost/tokens → matches
        # handle_dev._is_premodel_infra_failure, so the dev circuit breaker
        # re-dispatches without consuming the retry budget.
        return RunResult(
            success=False,
            error=err,
            summary=err,
            last_assistant_message="",
            recent_tool_calls=[],
            self_summary="",
        )
    except Exception as exc:
        err = f"sandbox run raised: {exc!r}"
        _record(
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
            duration_s=_elapsed(),
            story_id=story_id,
            model_tier=difficulty,
        )
        return RunResult(
            success=False,
            error=err,
            summary=err,
            last_assistant_message="",
            recent_tool_calls=[],
            self_summary="",
        )

    files_changed = _scan_repo_for_changed_files(Path(repo_path))
    test_passed, test_out = _run_pytest(Path(repo_path), test_command=test_command)
    self_summary = _extract_self_summary(last_assistant_message)

    _record(
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
        duration_s=_elapsed(),
        story_id=story_id,
        model_tier=difficulty,
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
        last_assistant_message=last_assistant_message,
        recent_tool_calls=recent_tool_calls,
        self_summary=self_summary,
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
    story_id: int | None = None,
    app: str | None = None,
    direction_id: str | None = None,
    model_tier: str | None = None,
    software_factory_root: Path | None = None,
) -> str | dict[str, Any]:
    """Single ``litellm.completion()`` call. Returns text, or a dict if ``schema`` set.

    When ``schema`` is provided, the prompt is augmented with an instruction
    to return JSON matching the schema; the response is parsed and validated
    via ``jsonschema`` if installed, falling back to a minimal key-presence
    check otherwise.
    """
    # Log prompt metadata (length, section headers, placeholder markers, hash)
    # to ``state/events/prompts.ndjson`` BEFORE any failure path — including
    # ``_resolve_api_key``, which can return None and raise, and the litellm
    # import below, which can ImportError. The metadata is needed precisely
    # when the call later fails, so the placeholder_prompts detector / L1
    # watcher can correlate a leaked-placeholder prompt with the resulting
    # error row in runs.ndjson. NEVER logs prompt content — only metadata.
    _log_prompt_metadata(
        persona=persona,
        prompt=prompt,
        model_id=model_id,
        story_id=story_id,
        software_factory_root=software_factory_root,
    )

    cfg = LLMConfig(model=model_id, api_key=api_key, base_url=base_url)
    resolved_key = _resolve_api_key(cfg)

    _t0 = time.monotonic()
    _started_at = datetime.now(UTC).isoformat()

    def _elapsed() -> float:
        return round(time.monotonic() - _t0, 3)

    def _record(**kw: Any) -> None:
        """Inject started_at + software_factory_root into every _record_run call."""
        _record_run(
            **kw,
            started_at=_started_at,
            software_factory_root=software_factory_root,
        )

    if dry_run:
        _record(
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
            duration_s=_elapsed(),
            story_id=story_id,
            model_tier=model_tier,
        )
        if schema is not None:
            return {"_dry_run": True, "persona": persona, "model": model_id}
        return f"[DRY-RUN text_run] persona={persona} model={model_id}"

    if resolved_key is None:
        msg = f"No API key available for {model_id!r}"
        _record(
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
            duration_s=_elapsed(),
            story_id=story_id,
            model_tier=model_tier,
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

    # Heartbeat for the whole text_run call (potentially multi-attempt). The
    # TUI sees this row while the LLM call is in flight, regardless of retry
    # loops inside this function. Use manual start/end so we don't have to
    # re-indent the multi-page retry block under a ``with`` clause.
    from factory.observability.heartbeat import end_heartbeat, start_heartbeat

    _hb_db = db_path or _DEFAULT_DB_PATH
    _hb_id: int | None = None
    with contextlib.suppress(Exception):
        _hb_id = start_heartbeat(
            _hb_db,
            persona=persona,
            model=model_id,
            mode="text",
            story_id=story_id,
            app=app,
            direction_id=direction_id,
        )

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
        attempt_out = int(usage.get("completion_tokens", 0) or 0)
        tokens_out += attempt_out
        try:
            cost_usd += float(getattr(response, "_hidden_params", {}).get("response_cost") or 0.0)
        except Exception:
            pass
        try:
            last_finish_reason = response["choices"][0].get("finish_reason")
        except Exception:
            last_finish_reason = None

        # A "length" finish_reason is only a REAL truncation if the model
        # actually emitted close to the cap. Some providers (observed:
        # deepseek-chat in JSON mode) intermittently return a tiny malformed
        # body while still flagging finish_reason="length"; doubling the cap
        # then re-calling cannot help — the model isn't using the cap it has.
        # Treat that as futile and stop retrying instead of burning the full
        # doubling ladder (8192 -> 65536) on a model that won't comply.
        fake_truncation = (
            last_finish_reason == "length" and attempt_out < int(current_max * 0.8)
        )

        if schema is None:
            # Plain text mode — only retry on REAL truncation.
            if (
                last_finish_reason != "length"
                or fake_truncation
                or current_max >= _MAX_OUTPUT_RETRY_CEILING
            ):
                break
        else:
            try:
                parsed = json.loads(text)
                break
            except Exception as parse_exc:
                if (
                    current_max >= _MAX_OUTPUT_RETRY_CEILING
                    or attempt == _MAX_OUTPUT_RETRIES
                    or fake_truncation
                ):
                    # No more headroom — record and raise with full diagnostics.
                    if _hb_id is not None:
                        with contextlib.suppress(Exception):
                            end_heartbeat(_hb_db, _hb_id)
                    _record(
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
                        duration_s=_elapsed(),
                        story_id=story_id,
                        model_tier=model_tier,
                    )
                    raise RuntimeError(
                        f"JSON-mode response was not valid JSON after "
                        f"{attempt} attempts (max_tokens up to {current_max}, "
                        f"finish_reason={last_finish_reason}): {parse_exc}"
                    ) from parse_exc

        # Double for next attempt; clamp to ceiling.
        current_max = min(current_max * 2, _MAX_OUTPUT_RETRY_CEILING)

    if _hb_id is not None:
        with contextlib.suppress(Exception):
            end_heartbeat(_hb_db, _hb_id)

    success = True

    _record(
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
        duration_s=_elapsed(),
        story_id=story_id,
        model_tier=model_tier,
    )

    if schema is not None:
        return cast(dict[str, Any], parsed)
    return cast(str, text)
