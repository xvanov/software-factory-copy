"""Independent acceptance-oracle authoring (WS1.2).

This module authors the acceptance test that the ``acceptance-verified`` gate
later runs. The whole point is INDEPENDENCE from the dev:

* Authored from the SPEC ONLY — the direction's acceptance criteria (+ its
  ``flow.md`` / ``api_spec.md`` if present) and the story title/scope. It is
  NEVER given the dev's implementation or the dev's tests.
* Authored EARLY — at story spawn (``handle_stories_spawned``), which runs at
  pm-sync time, long before the dev handler runs on a later tick. Freezing the
  test before the dev starts is the strongest anti-reward-hack posture: the dev
  cannot shape a test that already exists and that it never sees.
* Stored in FACTORY STATE — under ``state/acceptance/<app>/<story_id>/`` — which
  is outside the app repo and outside the per-story dev worktree (the worktree
  is a checkout of the app repo under ``state/worktrees/``; nothing copies
  factory ``state/acceptance/`` into it — see ``factory.chain.worktree``). The
  dev sandbox is handed only ``repo_path`` (the worktree) and never a pointer to
  this path, so it does not receive the acceptance test.

The authored path (relative to the factory root) is recorded on
``StoryRecord.acceptance_test_ref``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from factory.app_config import AppConfig
from factory.chain.state_machine import StoryRecord

if TYPE_CHECKING:
    from factory.directions.parser import Direction

_log = logging.getLogger(__name__)

# Injection seam for tests: an author function takes the assembled spec prompt
# and returns the python source of the acceptance test. Default is the real LLM
# call (``_llm_author``); tests pass a deterministic fake.
AuthorFn = Callable[[str, StoryRecord], str]

_ACCEPTANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["test_file_content"],
    "properties": {"test_file_content": {"type": "string"}},
}

# How many times to retry a flaky author call before giving up for this pass.
# Expected-but-unauthored stories are re-attempted again on later ticks by
# ``reauthor_missing_oracles`` — this just absorbs transient errors in one pass.
_AUTHOR_ATTEMPTS = 3


def acceptance_dir(software_factory_root: Path, app: str, story_id: int | None) -> Path:
    """The per-story directory holding the stored acceptance test."""
    sid = int(story_id) if story_id is not None else 0
    return Path(software_factory_root) / "state" / "acceptance" / app / str(sid)


def acceptance_expected_for(app_config: AppConfig, direction: Direction) -> bool:
    """Whether a story under ``direction`` MUST have an acceptance oracle.

    The single source of truth for the required/blocking decision — set at spawn
    INDEPENDENT of whether authoring later succeeds, so a flaky author cannot
    silently downgrade a story to "not required".
    """
    return bool(app_config.gates.acceptance_oracle and direction.acceptance)


def _emit(
    software_factory_root: Path,
    event: str,
    story: StoryRecord,
    **extra: Any,
) -> None:
    """Best-effort visibility event on the ``acceptance`` stream (never raises)."""
    try:
        from factory.manager.signals import write_event

        write_event(
            "acceptance",
            {"event": event, "app": story.app, "story_id": story.id,
             "direction_id": story.direction_id, **extra},
            software_factory_root=software_factory_root,
        )
    except Exception:  # noqa: BLE001 - telemetry path, never fail the caller
        pass


def _read_artifact(direction: Direction, name: str, present: bool) -> str:
    if not present:
        return ""
    try:
        return (direction.dir_path / name).read_text(encoding="utf-8").rstrip()
    except OSError:
        return ""


def build_spec_prompt(story: StoryRecord, direction: Direction) -> str:
    """Assemble the SPEC-ONLY prompt handed to the acceptance author.

    Contains the acceptance criteria verbatim plus any flow.md / api_spec.md the
    direction provides, and the story's title/scope. Deliberately contains NO
    implementation and NO dev tests — the author must write blind to the code.
    """
    acceptance_lines = list(direction.acceptance)
    ac_block = (
        "\n".join(f"{i + 1}. {ac}" for i, ac in enumerate(acceptance_lines))
        if acceptance_lines
        else "(no explicit acceptance criteria)"
    )
    flow_text = _read_artifact(direction, "flow.md", direction.has_flow)
    api_text = _read_artifact(direction, "api_spec.md", direction.has_api_spec)

    parts = [
        "## Story under acceptance",
        f"- Title: {story.title}",
        f"- Scope: {story.scope}",
        f"- App: {story.app}",
        "",
        "## Acceptance criteria (verbatim from the direction — the SPEC)",
        "",
        ac_block,
    ]
    if flow_text:
        parts += ["", "## Flow (verbatim from the direction)", "", flow_text]
    if api_text:
        parts += ["", "## API spec (verbatim from the direction)", "", api_text]
    return "\n".join(parts)


def _llm_author(spec_prompt: str, story: StoryRecord) -> str:
    """Real author: call the ``acceptance_author`` persona with the spec only."""
    from factory.model_router import route
    from factory.runner import _read_persona_prompt, text_run

    persona = "acceptance_author"
    persona_prompt = _read_persona_prompt(persona)
    full_prompt = (
        f"{persona_prompt.rstrip()}\n\n"
        "---\n\n"
        "## Input (SPEC ONLY — you are blind to any implementation)\n\n"
        f"{spec_prompt}\n\n"
        "---\n\n"
        "Return the JSON object with the acceptance test file content."
    )
    result = text_run(
        persona=persona,
        prompt=full_prompt,
        model_id=route(persona),
        schema=_ACCEPTANCE_SCHEMA,
        max_tokens=4096,
        story_id=story.id,
        app=story.app,
        direction_id=story.direction_id,
    )
    if not isinstance(result, dict):
        raise RuntimeError("acceptance_author text_run returned a non-dict for schema call")
    content = result.get("test_file_content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("acceptance_author returned empty test_file_content")
    return content


def _persist(story: StoryRecord, software_factory_root: Path, db_path: Path | None) -> None:
    try:
        from factory.chain.handlers import persist_story

        persist_story(story, db_path or (Path(software_factory_root) / "state" / "factory.db"))
    except Exception:  # noqa: BLE001 - flags are set in-memory even if the write hiccups
        pass


def author_acceptance_test(
    story: StoryRecord,
    direction: Direction,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    author_fn: AuthorFn | None = None,
) -> str | None:
    """Author + store the acceptance oracle for ``story``; return its ref.

    ALWAYS sets ``story.acceptance_expected`` (= app opted in AND the direction
    has ACs) and persists it — INDEPENDENT of whether authoring succeeds. This
    is what makes an authoring failure BLOCK rather than silently ship: the gate
    and ``required_gate_labels`` key off ``acceptance_expected``, not the ref.

    Returns the stored path (relative to ``software_factory_root``, also written
    to ``story.acceptance_test_ref``) on success, else ``None``:

    * app not opted in / direction has no ACs → expected=False, ref None; and
    * ``dry_run`` (no LLM) → expected set from spec, ref left None (a later
      real tick authors it); and
    * author fails after ``_AUTHOR_ATTEMPTS`` retries → expected stays True, ref
      None → gate BLOCKS and the tick self-heal re-authors later.

    Independence is structural: the prompt is SPEC-ONLY (``build_spec_prompt``)
    and the file lands under ``state/acceptance/`` — outside the dev worktree.
    """
    expected = acceptance_expected_for(app_config, direction)
    story.acceptance_expected = expected
    if not expected:
        _persist(story, software_factory_root, db_path)
        return None
    if dry_run:
        # No LLM in dry-run; record the expectation so the gate correctly
        # blocks (expected but no oracle) until a real tick authors it.
        _persist(story, software_factory_root, db_path)
        return None

    author = author_fn or _llm_author
    spec_prompt = build_spec_prompt(story, direction)
    content: str | None = None
    last_err: str | None = None
    for attempt in range(1, _AUTHOR_ATTEMPTS + 1):
        try:
            content = author(spec_prompt, story)
            break
        except Exception as exc:  # noqa: BLE001 - retry transient author failures
            last_err = repr(exc)[:300]
            _log.warning(
                "acceptance author failed (story=%s attempt=%d/%d): %s",
                story.id, attempt, _AUTHOR_ATTEMPTS, last_err,
            )

    if content is None:
        # Expected but authoring flaked. Leave ref None; expected stays True so
        # the gate blocks and reauthor_missing_oracles retries on a later tick.
        _persist(story, software_factory_root, db_path)
        _emit(
            software_factory_root, "author_failed", story,
            attempts=_AUTHOR_ATTEMPTS, error=last_err,
        )
        return None

    out_dir = acceptance_dir(software_factory_root, story.app, story.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    test_path = out_dir / "test_acceptance.py"
    test_path.write_text(content, encoding="utf-8")

    rel = test_path.relative_to(Path(software_factory_root))
    story.acceptance_test_ref = str(rel)
    _persist(story, software_factory_root, db_path)
    _emit(software_factory_root, "authored", story, ref=str(rel))
    return str(rel)


def ref_is_readable(story: StoryRecord | None, software_factory_root: Path | None) -> bool:
    """True when the story's stored acceptance test exists on disk."""
    if story is None or software_factory_root is None:
        return False
    ref = story.acceptance_test_ref
    if not ref:
        return False
    p = Path(ref)
    stored = p if p.is_absolute() else Path(software_factory_root) / p
    return stored.exists()


def reauthor_missing_oracles(
    app: str,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    author_fn: AuthorFn | None = None,
) -> int:
    """Self-heal pass: (re-)author acceptance oracles for stories that are
    EXPECTED to have one but whose stored test is missing (authoring flaked on
    a previous tick). Returns the number newly authored.

    Runs early in the tick, before the story chain advances and before merge
    evaluation — so a story that blocked on ``acceptance-verified`` last tick
    gets its oracle back this tick. Re-authoring is always SPEC-ONLY
    (``build_spec_prompt`` via a freshly-resolved Direction), so it stays blind
    to the dev's code no matter how late it happens — independence is preserved.

    Best-effort: never raises. No-op in dry-run (no LLM).
    """
    if dry_run:
        return 0
    root = Path(software_factory_root)
    db = db_path or (root / "state" / "factory.db")
    try:
        from sqlmodel import Session, select

        from factory.chain.handlers import _engine, find_direction_for_story
    except Exception:  # noqa: BLE001
        return 0

    try:
        eng = _engine(db)
        with Session(eng) as session:
            candidates = list(
                session.exec(
                    select(StoryRecord).where(
                        StoryRecord.app == app,
                        StoryRecord.acceptance_expected == True,  # noqa: E712 - SQL bool
                    )
                ).all()
            )
    except Exception:  # noqa: BLE001
        return 0

    healed = 0
    for story in candidates:
        if ref_is_readable(story, root):
            continue
        direction = None
        try:
            direction = find_direction_for_story(story, root)
        except Exception:  # noqa: BLE001
            direction = None
        if direction is None:
            _emit(root, "reauthor_no_direction", story)
            continue
        try:
            app_config = _load_app_config(app, root)
        except Exception:  # noqa: BLE001
            continue
        ref = author_acceptance_test(
            story, direction, app_config, root,
            dry_run=False, db_path=db, author_fn=author_fn,
        )
        if ref is not None:
            healed += 1
            _emit(root, "reauthored", story, ref=ref)
    return healed


def _load_app_config(app: str, software_factory_root: Path) -> AppConfig:
    from factory.app_config import load_app_config

    return load_app_config(app, software_factory_root)


__all__ = [
    "acceptance_dir",
    "acceptance_expected_for",
    "author_acceptance_test",
    "build_spec_prompt",
    "reauthor_missing_oracles",
    "ref_is_readable",
]
