"""Chain handlers — one per state transition.

Each handler:
  * Takes ``(story, app_config, software_factory_root, *, dry_run=False)``.
  * Returns a ``HandlerResult`` carrying the next state, an opaque payload,
    and an optional error.
  * Is responsible for ALL side effects of its transition: LLM calls,
    GitHub API calls, filesystem writes, DB writes.
  * In ``dry_run=True`` mode, MUST NOT make any LLM calls, MUST NOT touch
    GitHub, MUST NOT write to the app repo. The deterministic fixture
    payload exercises the downstream state shape.

The orchestrator (``factory/chain/orchestrator.py``) decides which handler
to invoke based on the story's current state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlmodel import Session, SQLModel, create_engine, select

from factory.app_config import AppConfig
from factory.chain.state_machine import (
    EVENT_DEV_EXHAUSTED,
    EVENT_DEV_STARTED,
    EVENT_DEV_TESTS_GREEN,
    EVENT_DEV_TESTS_RED,
    EVENT_DOCS_ENFORCER_CHECK,
    EVENT_DOCS_ENFORCER_FAIL,
    EVENT_DOCS_ENFORCER_PASS,
    EVENT_REVIEWER_APPROVE,
    EVENT_REVIEWER_REQUEST_CHANGES,
    EVENT_REVIEWER_STARTED,
    EVENT_SM_DONE,
    EVENT_SM_STARTED,
    EVENT_TECH_WRITER_DONE,
    EVENT_TECH_WRITER_STARTED,
    EVENT_TEST_DESIGN_DONE,
    EVENT_TEST_DESIGN_STARTED,
    EVENT_TEST_IMPL_SLOP,
    EVENT_TEST_IMPL_STARTED,
    EVENT_TESTS_RED,
    StoryRecord,
    StoryState,
    advance,
)
from factory.context.enforcer import format_violation_comment, scan_pr_diff
from factory.context.updater import ContextUpdate, apply_context_updates
from factory.directions.parser import Direction, list_direction_dirs, parse_direction_dir
from factory.model_router import route

# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass
class HandlerResult:
    """Outcome of a single handler invocation."""

    next_state: StoryState
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #


_MIGRATION_COLUMNS: dict[str, str] = {
    # column_name -> SQL type (idempotent ALTER TABLE ADD COLUMN if missing).
    "sm_result_json": "TEXT",
    "last_rejection_reason": "TEXT",
}


def _ensure_story_columns(eng: Any) -> None:
    """Idempotently add Phase 3 columns to the ``stories`` table if missing.

    SQLModel.metadata.create_all() only creates tables that don't exist; it
    won't add new columns to an existing table. For dev we don't need full
    Alembic — just an ``ALTER TABLE ADD COLUMN`` for the small set of new
    columns Phase 3 introduces. Adding a column twice raises; we suppress
    that specific failure.
    """
    from sqlalchemy import text

    with eng.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(stories)")).fetchall()
        existing = {r[1] for r in rows}
        for col, sqltype in _MIGRATION_COLUMNS.items():
            if col in existing:
                continue
            conn.execute(text(f"ALTER TABLE stories ADD COLUMN {col} {sqltype}"))


def _engine(db_path: Path) -> Any:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(f"sqlite:///{db_path}", echo=False)
    SQLModel.metadata.create_all(eng)
    _ensure_story_columns(eng)
    return eng


def persist_story(story: StoryRecord, db_path: Path) -> StoryRecord:
    """Insert-or-update ``story`` in ``state/factory.db.stories``."""
    eng = _engine(db_path)
    from datetime import UTC, datetime

    story.updated_at = datetime.now(UTC).isoformat()
    with Session(eng) as session:
        session.add(story)
        session.commit()
        session.refresh(story)
    return story


def get_story(story_id: int, db_path: Path) -> StoryRecord | None:
    eng = _engine(db_path)
    with Session(eng) as session:
        return session.get(StoryRecord, story_id)


def stories_in_flight(app: str, db_path: Path) -> list[StoryRecord]:
    """Return every story whose state is not terminal.

    Phase 5 adds DEPLOY_PENDING as an active state (the orchestrator drives
    it to DEPLOYED or BLOCKED_DEPLOY_FAILED). DEPLOYED and
    BLOCKED_DEPLOY_FAILED are terminal — the chain stops driving once a PR
    has been deployed (or its deploy has failed and waits for human
    intervention).
    """
    eng = _engine(db_path)
    with Session(eng) as session:
        rows = session.exec(select(StoryRecord).where(StoryRecord.app == app)).all()
    # Terminal: PR_OPEN through READY_FOR_MERGE (the auto-merge worker drives
    # these by polling, not the orchestrator tick), DEPLOYED, and the two
    # blocked states.
    terminal = {
        StoryState.PR_OPEN.value,
        StoryState.CI_PENDING.value,
        StoryState.CI_GREEN.value,
        StoryState.READY_FOR_MERGE.value,
        StoryState.DEPLOYED.value,
        StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value,
        StoryState.BLOCKED_DEPLOY_FAILED.value,
    }
    # DEPLOY_PENDING is INTENTIONALLY in-flight so the orchestrator's
    # _DISPATCH picks it up for handle_deploy.
    return [r for r in rows if r.state not in terminal]


# --------------------------------------------------------------------------- #
# stories_spawned (from PM result) — invoked by the orchestrator
# --------------------------------------------------------------------------- #


_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def _slug_of(text: str) -> str:
    s = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return (s[:60] or "story").strip("-") or "story"


def handle_stories_spawned(
    direction: Direction,
    pm_result: dict[str, Any],
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    github_client: Any = None,
) -> list[StoryRecord]:
    """For each ``child_story`` in ``pm_result``, create a StoryRecord, optionally
    open the GH issue, and write the story file shell to the app repo (if any).

    In dry-run mode:
      * No GH issue is created; ``github_issue_number`` is ``None``.
      * No file is written to the app repo; ``story_file_path`` is a
        plausible placeholder under ``stories/0-<slug>.md``.
    """
    db = db_path or (software_factory_root / "state" / "factory.db")
    out: list[StoryRecord] = []
    child_stories = pm_result.get("child_stories") or []
    for child in child_stories:
        slug = _slug_of(child.get("title") or "story")
        title = str(child.get("title") or "Untitled story")[:200]
        scope = str(child.get("scope") or "backend")
        issue_number: int | None = None
        story_file_path = f"stories/0-{slug}.md"

        if not dry_run and github_client is not None:
            # Real-run: open GH issue, then story_file_path uses the issue number.
            repo = github_client.get_repo(app_config.repo)
            body = (
                f"{child.get('rationale') or ''}\n\n"
                f"_Direction: `{direction.id}-{direction.slug}` "
                f"(tracker: #{direction.state.get('tracker_issue', '?')})._\n"
            )
            issue = repo.create_issue(
                title=title,
                body=body,
                labels=["story", f"scope/{scope}"],
            )
            issue_number = int(issue.number)
            story_file_path = f"stories/{issue_number}-{slug}.md"

        story = StoryRecord(
            direction_id=direction.id or direction.slug,
            app=direction.app,
            title=title,
            slug=slug,
            scope=scope,
            state=StoryState.STORY_CREATED.value,
            github_issue_number=issue_number,
            github_branch=f"story/{issue_number or 0}-{slug}",
            story_file_path=story_file_path,
        )
        persist_story(story, db)
        out.append(story)
    return out


# --------------------------------------------------------------------------- #
# sm (Scrum Master)
# --------------------------------------------------------------------------- #


# Matches a story filename like "0-my-slug.md" or "42-some-thing.md" — the
# leading numeric prefix is the issue number (0 = "no issue yet" placeholder).
_STORY_FILENAME_RE = re.compile(r"^(\d+)-(.+\.md)$")


def _substitute_issue_number_in_path(
    target_path_rel: str,
    *,
    issue_number: int | None,
    slug: str,
) -> str:
    """Substitute the leading ``\\d+-`` prefix in the story filename with the
    real ``issue_number`` when known.

    Robust to any directory prefix on ``target_path_rel`` (the path's
    ``parent`` is preserved verbatim). If ``issue_number`` is None, the path
    is returned unchanged. If the filename doesn't carry a numeric prefix at
    all, we fall back to ``stories/<issue_number>-<slug>.md`` so the chain
    still finds the file by convention.
    """
    if issue_number is None:
        return target_path_rel
    path = Path(target_path_rel)
    m = _STORY_FILENAME_RE.match(path.name)
    if m is not None:
        # Only substitute when the existing prefix is the "no issue yet"
        # placeholder (``0``). A real issue number that disagrees with the
        # story's ``github_issue_number`` is left alone — this is more likely
        # a chain bug (split story?) than the SM's intent.
        if m.group(1) == "0":
            new_name = f"{issue_number}-{m.group(2)}"
            return str(path.with_name(new_name))
        return target_path_rel
    # No numeric prefix; conventionalize.
    return f"stories/{issue_number}-{slug}.md"


def _dry_run_sm(
    story: StoryRecord, direction: Direction | None, software_factory_root: Path
) -> dict[str, Any]:
    """Deterministic Scrum-Master result for dry-run mode.

    Produces a BMAD-format story file embedding:
      * the direction's flow.md verbatim if present
      * the direction's api_spec.md verbatim if present
      * acceptance criteria verbatim
      * pointers to context/current-state.md and context/modules/<scope>.md.
    """
    flow_text = ""
    api_text = ""
    acceptance_lines: list[str] = []
    if direction is not None:
        if direction.has_flow:
            try:
                flow_text = (direction.dir_path / "flow.md").read_text(encoding="utf-8").rstrip()
            except FileNotFoundError:
                flow_text = ""
        if direction.has_api_spec:
            try:
                api_text = (direction.dir_path / "api_spec.md").read_text(encoding="utf-8").rstrip()
            except FileNotFoundError:
                api_text = ""
        acceptance_lines = list(direction.acceptance)

    ac_block = (
        "\n".join(f"{i + 1}. {ac}" for i, ac in enumerate(acceptance_lines))
        if acceptance_lines
        else "1. (no explicit acceptance criteria — see Dev Notes)"
    )

    dev_notes_parts = [
        "- Story carries verbatim embeds of user-supplied artifacts below.",
        f"- Read context/current-state.md and context/modules/{story.scope}.md before "
        "implementing.",
        "[Source: context/current-state.md]",
        f"[Source: context/modules/{story.scope}.md]",
    ]
    if flow_text:
        dev_notes_parts.append("\n#### Flow (verbatim from direction)\n\n" + flow_text)
    if api_text:
        dev_notes_parts.append("\n#### API spec (verbatim from direction)\n\n" + api_text)
    dev_notes_parts.append("\n#### Acceptance criteria (verbatim from direction)\n\n" + ac_block)
    dev_notes = "\n".join(dev_notes_parts)

    file_content = (
        f"# Story 1.1: {story.title}\n\n"
        "Status: ready-for-dev\n\n"
        "## Story\n\n"
        f"As a user, I want {story.title.lower()}, so that the documented outcome holds.\n\n"
        "## Acceptance Criteria\n\n"
        f"{ac_block}\n\n"
        "## Tasks / Subtasks\n\n"
        "- [ ] Task 1 (AC: #1)\n"
        "  - [ ] Subtask 1.1\n\n"
        "## Dev Notes\n\n"
        f"{dev_notes}\n\n"
        "### References\n\n"
        f"- [Source: context/modules/{story.scope}.md]\n"
        "- [Source: context/current-state.md]\n\n"
        "## Dev Agent Record\n\n"
        "### Agent Model Used\n\n"
        "(populated by dev)\n\n"
        "### File List\n\n"
        "## Senior Developer Review\n\n"
        "## Review Follow-ups\n"
    )

    return {
        "stories": [
            {
                "title": story.title,
                "slug": story.slug,
                "scope": story.scope,
                "file_content": file_content,
                "target_path": story.story_file_path
                or f"stories/{story.github_issue_number or 0}-{story.slug}.md",
            }
        ],
        "summary": f"Dry-run SM story for {story.slug!r}.",
    }


_SM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["stories", "summary"],
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "slug", "scope", "file_content", "target_path"],
                "properties": {
                    "title": {"type": "string"},
                    "slug": {"type": "string"},
                    "scope": {"type": "string"},
                    "file_content": {"type": "string"},
                    "target_path": {"type": "string"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}


def handle_sm(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    fixture: dict[str, Any] | None = None,
) -> HandlerResult:
    """Run the Scrum-Master persona; write the BMAD story file to disk.

    In dry-run mode: deterministic fixture story is composed from the
    direction's flow.md / api_spec.md / acceptance criteria (if any). The
    story file is written to ``apps/<app>/stories/<issue|0>-<slug>.md``.
    In real-run mode: invokes ``text_run("sm", ...)`` with the SM persona
    prompt + context prelude + PM result + Direction. The chain writes the
    file content from the SM result.
    """
    db = db_path or (software_factory_root / "state" / "factory.db")
    story.state = advance(story, EVENT_SM_STARTED).value
    persist_story(story, db)

    direction = find_direction_for_story(story, software_factory_root)

    if fixture is not None:
        result = fixture
    elif dry_run:
        result = _dry_run_sm(story, direction, software_factory_root)
    else:
        from factory.context.loader import compose_context_prelude
        from factory.runner import text_run

        persona = "sm"
        persona_prompt = _read_persona_prompt(persona)
        prelude = compose_context_prelude(
            persona=persona,
            app_repo_path=software_factory_root / "apps" / story.app,
            task_scope=story.scope,
        )
        flow_text = ""
        api_text = ""
        direction_body = ""
        pm_block = ""
        if direction is not None:
            direction_body = direction.raw_body
            if direction.has_flow:
                try:
                    flow_text = (direction.dir_path / "flow.md").read_text(encoding="utf-8")
                except FileNotFoundError:
                    flow_text = ""
            if direction.has_api_spec:
                try:
                    api_text = (direction.dir_path / "api_spec.md").read_text(encoding="utf-8")
                except FileNotFoundError:
                    api_text = ""
            pm_result = direction.state.get("pm_result") or {}
            try:
                pm_block = json.dumps(pm_result, indent=2)
            except TypeError:
                pm_block = "(pm_result not JSON-serializable)"

        full_prompt = (
            f"{persona_prompt.rstrip()}\n\n"
            "---\n\n"
            "## Context\n\n"
            f"{prelude.rstrip()}\n\n"
            "## PM result\n\n"
            f"```json\n{pm_block}\n```\n\n"
            "## Direction\n\n"
            f"{direction_body.rstrip()}\n\n"
            f"### flow.md\n\n{flow_text.rstrip() if flow_text else '(none)'}\n\n"
            f"### api_spec.md\n\n{api_text.rstrip() if api_text else '(none)'}\n\n"
            "## Story metadata\n\n"
            f"- title: {story.title}\n"
            f"- slug: {story.slug}\n"
            f"- scope: {story.scope}\n"
            f"- target_path: {story.story_file_path}\n\n"
            "Return the JSON object. No prose outside the JSON."
        )
        model_id = route(persona)
        result_any = text_run(
            persona=persona,
            prompt=full_prompt,
            model_id=model_id,
            schema=_SM_SCHEMA,
            max_tokens=_STRONG_MAX_TOKENS,
        )
        if not isinstance(result_any, dict):
            return HandlerResult(
                next_state=StoryState(story.state),
                error="sm returned non-dict",
            )
        result = result_any

    # Find the story entry that matches this StoryRecord (by slug, fall back to first).
    stories_out = result.get("stories") or []
    matched: dict[str, Any] | None = None
    for s in stories_out:
        if s.get("slug") == story.slug:
            matched = s
            break
    if matched is None and stories_out:
        matched = stories_out[0]

    if matched is None:
        story.error = "sm produced no stories"
        story.state = advance(story, EVENT_SM_DONE).value
        story.sm_result_json = json.dumps(result)
        persist_story(story, db)
        return HandlerResult(
            next_state=StoryState(story.state),
            payload=result,
            error=story.error,
        )

    # Resolve the on-disk target path. Prefer the SM's emitted target_path; if
    # the filename leads with a numeric prefix (the SM's "<issue-number>"
    # placeholder; ``0-`` in dry-run, but could be any digits a stale fixture
    # carries) and we now know the real issue number, substitute it. Robust
    # to any directory prefix (``stories/...``, ``apps/<x>/stories/...``).
    target_path_rel = str(matched.get("target_path") or story.story_file_path)
    target_path_rel = _substitute_issue_number_in_path(
        target_path_rel,
        issue_number=story.github_issue_number,
        slug=story.slug,
    )

    target_abs = software_factory_root / "apps" / story.app / target_path_rel
    target_abs.parent.mkdir(parents=True, exist_ok=True)
    target_abs.write_text(matched.get("file_content") or "", encoding="utf-8")
    story.story_file_path = target_path_rel
    story.sm_result_json = json.dumps(result)
    story.state = advance(story, EVENT_SM_DONE).value
    persist_story(story, db)
    return HandlerResult(next_state=StoryState(story.state), payload=result)


# --------------------------------------------------------------------------- #
# test_design
# --------------------------------------------------------------------------- #


# Cap tokens per persona class — controls cost on real provider calls.
_CHEAP_MAX_TOKENS = 2048
_STRONG_MAX_TOKENS = 4096


_TEST_DESIGN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["test_plan", "e2e_required", "summary"],
    "properties": {
        "test_plan": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "name",
                    "what_it_asserts",
                    "tool",
                    "file_path",
                    "key_steps",
                    "why_meaningful",
                ],
            },
        },
        "e2e_required": {"type": "boolean"},
        "summary": {"type": "string"},
    },
}


def _read_persona_prompt(persona: str) -> str:
    """Defer to the runner's reader to avoid duplicating the personas dir constant."""
    from factory.runner import _read_persona_prompt as _rpp

    return _rpp(persona)


def _dry_run_test_design(story: StoryRecord) -> dict[str, Any]:
    """Deterministic test plan for dry-run mode."""
    base_path = "tests" if story.scope != "frontend" else "e2e"
    tool = "pytest" if story.scope != "frontend" else "playwright"
    file_ext = ".py" if tool == "pytest" else ".spec.ts"
    return {
        "test_plan": [
            {
                "name": f"test_{story.slug.replace('-', '_')}_happy_path",
                "what_it_asserts": (
                    f"{story.title} produces the documented outcome when invoked with "
                    "the canonical input."
                ),
                "tool": tool,
                "file_path": f"{base_path}/test_{story.slug.replace('-', '_')}{file_ext}",
                "key_steps": [
                    "arrange: prepare the canonical input",
                    "act: exercise the subject under test",
                    "assert: verify the documented outcome",
                ],
                "why_meaningful": (
                    "If this test goes red, the user-facing behavior the story names is broken."
                ),
            }
        ],
        "e2e_required": story.scope == "frontend",
        "summary": f"Dry-run test plan for story {story.slug!r}.",
    }


def handle_test_design(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
) -> HandlerResult:
    """Run the Test-Designer persona to produce a structured test plan."""
    db = db_path or (software_factory_root / "state" / "factory.db")

    # Advance to in-progress first.
    story.state = advance(story, EVENT_TEST_DESIGN_STARTED).value
    persist_story(story, db)

    if dry_run:
        plan = _dry_run_test_design(story)
    else:
        from factory.context.loader import compose_context_prelude
        from factory.runner import text_run

        persona = "test_designer"
        persona_prompt = _read_persona_prompt(persona)
        prelude = compose_context_prelude(
            persona=persona,
            app_repo_path=software_factory_root / "apps" / story.app,
            task_scope=story.scope,
        )
        story_file_content = ""
        story_file_real = software_factory_root / "apps" / story.app / story.story_file_path
        if story_file_real.exists():
            story_file_content = story_file_real.read_text(encoding="utf-8")
        full_prompt = (
            f"{persona_prompt.rstrip()}\n\n"
            "---\n\n"
            "## Context\n\n"
            f"{prelude.rstrip()}\n\n"
            "## Story\n\n"
            f"{story_file_content}\n\n"
            "---\n\n"
            "Return the JSON object for the test plan. No prose outside the JSON."
        )
        model_id = route(persona)
        result = text_run(
            persona=persona,
            prompt=full_prompt,
            model_id=model_id,
            schema=_TEST_DESIGN_SCHEMA,
            max_tokens=_STRONG_MAX_TOKENS,
        )
        if not isinstance(result, dict):
            return HandlerResult(
                next_state=StoryState(story.state),
                error="test_designer returned non-dict",
            )
        plan = result

    story.test_plan_json = json.dumps(plan)
    story.state = advance(story, EVENT_TEST_DESIGN_DONE).value
    persist_story(story, db)
    return HandlerResult(next_state=StoryState(story.state), payload={"plan": plan})


# --------------------------------------------------------------------------- #
# test_implementation
# --------------------------------------------------------------------------- #


def _dry_run_test_implementation(story: StoryRecord, plan: dict[str, Any]) -> dict[str, Any]:
    """Deterministic test-implementer result for dry-run mode.

    Returns exit_code=1 (red) and slop_detected=False — the desired
    happy-path outcome for a freshly-designed test suite against
    unimplemented code.
    """
    files = [t["file_path"] for t in plan.get("test_plan", [])]
    return {
        "files_written": files,
        "test_command_run": "<dry-run: not executed>",
        "exit_code": 1,
        "slop_detected": False,
        "output_excerpt": "(dry-run; no tests actually executed)",
        "summary": f"Dry-run wrote {len(files)} test file path(s).",
    }


def handle_test_implementation(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
) -> HandlerResult:
    """Run the Test-Implementer persona; observe RED; transition state."""
    db = db_path or (software_factory_root / "state" / "factory.db")
    story.state = advance(story, EVENT_TEST_IMPL_STARTED).value
    persist_story(story, db)

    plan_raw = story.test_plan_json or "{}"
    try:
        plan = json.loads(plan_raw)
    except json.JSONDecodeError:
        plan = {"test_plan": []}

    if dry_run:
        result = _dry_run_test_implementation(story, plan)
    else:
        # Real-run: invoke sandbox_run with the test_implementer persona
        # against the app repo's feature branch. The sandbox actually
        # writes the test files; we then ask it to run the test_command.
        from factory.runner import LLMConfig, sandbox_run

        # Locate the app repo. Phase-2 acceptance is dry-run only; real-run
        # path is plausible-on-inspection.
        repo_path = software_factory_root / "apps" / story.app
        story_file_path_obj = repo_path / story.story_file_path
        llm = LLMConfig(model=route("test_implementer"))
        import asyncio

        run_res = asyncio.run(
            sandbox_run(
                persona="test_implementer",
                story_path=story_file_path_obj,
                repo_path=repo_path,
                llm_config=llm,
                dry_run=False,
            )
        )
        # The sandbox returned ok if tests are red (which is the desired outcome).
        result = {
            "files_written": run_res.files_changed,
            "test_command_run": app_config.deploy.health_check_command or "(test_command)",
            "exit_code": 0 if run_res.test_run_passed else 1,
            "slop_detected": bool(run_res.test_run_passed),
            "output_excerpt": run_res.summary[-2000:],
            "summary": run_res.error or "test_implementer completed",
        }

    story.test_implementer_result_json = json.dumps(result)

    if result.get("slop_detected"):
        story.state = advance(story, EVENT_TEST_IMPL_SLOP).value
        story.error = "tests passed pre-implementation (slop)"
        persist_story(story, db)
        return HandlerResult(
            next_state=StoryState(story.state),
            payload=result,
            error=story.error,
        )

    # Red is the desired outcome.
    story.state = advance(story, EVENT_TESTS_RED).value
    persist_story(story, db)
    return HandlerResult(next_state=StoryState(story.state), payload=result)


# --------------------------------------------------------------------------- #
# dev
# --------------------------------------------------------------------------- #


_MAX_DEV_RETRIES = 3


def _dry_run_dev(story: StoryRecord) -> tuple[bool, dict[str, Any]]:
    """Deterministic dev result for dry-run mode.

    Returns (tests_green, payload). Default happy path: tests green after
    the first dev run.
    """
    return True, {
        "files_changed": [f"src/{story.slug.replace('-', '_')}.py"],
        "test_run_passed": True,
        "summary": "Dry-run dev pass: tests green on first attempt.",
    }


def handle_dev(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    force_red: bool = False,
) -> HandlerResult:
    """Run the Dev persona; check tests; retry/escalate on failure.

    ``force_red`` is for testing the retry path: when True, the dry-run
    branch returns tests_green=False so the handler exercises the retry +
    escalation logic.
    """
    db = db_path or (software_factory_root / "state" / "factory.db")
    story.state = advance(story, EVENT_DEV_STARTED).value
    persist_story(story, db)

    if dry_run:
        tests_green = not force_red
        _, payload = _dry_run_dev(story)
        if force_red:
            payload["test_run_passed"] = False
            payload["summary"] = "Dry-run dev failure: tests still red."
    else:
        from factory.runner import LLMConfig, sandbox_run

        repo_path = software_factory_root / "apps" / story.app
        story_file_path_obj = repo_path / story.story_file_path
        difficulty = story.current_model_tier
        llm = LLMConfig(model=route("dev", difficulty=difficulty))
        import asyncio

        run_res = asyncio.run(
            sandbox_run(
                persona="dev",
                story_path=story_file_path_obj,
                repo_path=repo_path,
                llm_config=llm,
                difficulty=difficulty,
                dry_run=False,
            )
        )
        tests_green = bool(run_res.test_run_passed)
        payload = {
            "files_changed": run_res.files_changed,
            "test_run_passed": tests_green,
            "summary": run_res.summary[-2000:],
        }

    if tests_green:
        story.state = advance(story, EVENT_DEV_TESTS_GREEN).value
        persist_story(story, db)
        return HandlerResult(next_state=StoryState(story.state), payload=payload)

    # Not green — bump retries.
    story.dev_retries += 1
    if story.dev_retries >= _MAX_DEV_RETRIES:
        story.state = advance(story, EVENT_DEV_EXHAUSTED).value
        story.error = f"dev exhausted retries ({story.dev_retries})"
        persist_story(story, db)
        return HandlerResult(next_state=StoryState(story.state), payload=payload, error=story.error)

    # Escalate model tier on retry (standard -> hard).
    if story.current_model_tier == "standard":
        story.current_model_tier = "hard"
    story.state = advance(story, EVENT_DEV_TESTS_RED).value
    persist_story(story, db)
    return HandlerResult(next_state=StoryState(story.state), payload=payload)


# --------------------------------------------------------------------------- #
# review
# --------------------------------------------------------------------------- #


def _dry_run_review(story: StoryRecord) -> dict[str, Any]:
    return {
        "verdict": "approve",
        "findings": [],
        "test_quality_score": 0.92,
        "test_quality_findings": [],
        "comments_to_post": [],
        "summary": "Dry-run reviewer: approve.",
    }


def handle_review(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    fixture: dict[str, Any] | None = None,
    github_client: Any = None,
) -> HandlerResult:
    """Run the Reviewer persona; post inline PR comments; transition state.

    ``fixture`` lets tests inject a specific reviewer JSON (e.g. a
    low-test-quality-score scenario).
    """
    db = db_path or (software_factory_root / "state" / "factory.db")
    story.state = advance(story, EVENT_REVIEWER_STARTED).value
    persist_story(story, db)

    if fixture is not None:
        result = fixture
    elif dry_run:
        result = _dry_run_review(story)
    else:
        from factory.context.loader import compose_context_prelude
        from factory.runner import text_run

        persona = "reviewer"
        persona_prompt = _read_persona_prompt(persona)
        prelude = compose_context_prelude(
            persona=persona,
            app_repo_path=software_factory_root / "apps" / story.app,
            task_scope=story.scope,
        )
        full_prompt = (
            f"{persona_prompt.rstrip()}\n\n"
            "---\n\n"
            "## Context\n\n"
            f"{prelude.rstrip()}\n\n"
            "## Story\n\n"
            f"(see {story.story_file_path})\n\n"
            "## Test plan\n\n"
            f"{story.test_plan_json or '{}'}\n\n"
            "## Test-Implementer result\n\n"
            f"{story.test_implementer_result_json or '{}'}\n\n"
            "## PR diff\n\n"
            "(fetched from GitHub by the chain — placeholder for real-run)\n\n"
            "Return the JSON object for the review. No prose outside the JSON."
        )
        model_id = route(persona)
        result_any = text_run(
            persona=persona,
            prompt=full_prompt,
            model_id=model_id,
            schema=None,  # reviewer output is JSON but we don't enforce schema here
            max_tokens=_STRONG_MAX_TOKENS,
        )
        try:
            result = json.loads(result_any) if isinstance(result_any, str) else result_any
        except (TypeError, json.JSONDecodeError):
            result = {"verdict": "request_changes", "summary": "reviewer JSON parse failed"}

    story.reviewer_result_json = json.dumps(result)

    # Post inline comments (real-run only).
    if not dry_run and github_client is not None and story.github_pr_number is not None:
        try:
            repo = github_client.get_repo(app_config.repo)
            pr = repo.get_pull(story.github_pr_number)
            for c in result.get("comments_to_post") or []:
                # Inline comment requires commit_sha, path, position; we
                # use a simpler issue comment in Phase 2 as fallback.
                pr.create_issue_comment(
                    f"**Reviewer note** ({c.get('file')}:{c.get('line')}):\n\n{c.get('body')}"
                )
        except Exception:  # pragma: no cover - real-run path
            pass

    verdict = result.get("verdict", "request_changes")
    score = float(result.get("test_quality_score", 0.0))
    if verdict == "approve" and score >= 0.7:
        story.state = advance(story, EVENT_REVIEWER_APPROVE).value
    else:
        story.state = advance(story, EVENT_REVIEWER_REQUEST_CHANGES).value
        # Label the PR if it's the test-quality branch (real-run only).
        if not dry_run and github_client is not None and story.github_pr_number is not None:
            try:
                repo = github_client.get_repo(app_config.repo)
                pr = repo.get_pull(story.github_pr_number)
                if score < 0.7:
                    pr.add_to_labels("needs-test-quality-fix")
                else:
                    pr.add_to_labels("needs-changes")
            except Exception:  # pragma: no cover - real-run path
                pass

    persist_story(story, db)
    return HandlerResult(next_state=StoryState(story.state), payload=result)


# --------------------------------------------------------------------------- #
# tech_writer
# --------------------------------------------------------------------------- #


def _dry_run_tech_writer(story: StoryRecord) -> dict[str, Any]:
    """Deterministic tech_writer result for dry-run mode."""
    return {
        "context_updates": [
            {
                "path": f"context/modules/{story.scope}.md",
                "action": "rewrite",
                "content": (
                    f"# {story.scope} module\n\nUpdated to reflect story `{story.slug}`.\n"
                ),
            }
        ],
        "rationale": "Dry-run tech_writer: minimal context refresh.",
    }


def handle_tech_writer(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    fixture: dict[str, Any] | None = None,
) -> HandlerResult:
    """Run the Tech-Writer persona; apply context updates to the app repo."""
    db = db_path or (software_factory_root / "state" / "factory.db")
    story.state = advance(story, EVENT_TECH_WRITER_STARTED).value
    persist_story(story, db)

    if fixture is not None:
        result = fixture
    elif dry_run:
        result = _dry_run_tech_writer(story)
    else:
        from factory.context.loader import compose_context_prelude
        from factory.runner import text_run

        persona = "tech_writer"
        persona_prompt = _read_persona_prompt(persona)
        prelude = compose_context_prelude(
            persona=persona,
            app_repo_path=software_factory_root / "apps" / story.app,
            task_scope=story.scope,
        )
        full_prompt = (
            f"{persona_prompt.rstrip()}\n\n"
            "---\n\n"
            "## Context\n\n"
            f"{prelude.rstrip()}\n\n"
            "## Story\n\n"
            f"(see {story.story_file_path})\n\n"
            "## PR diff\n\n"
            "(fetched from GitHub by the chain — placeholder for real-run)\n\n"
            "Return the JSON object. No prose outside the JSON."
        )
        model_id = route(persona)
        result_any = text_run(
            persona=persona,
            prompt=full_prompt,
            model_id=model_id,
            schema=None,
            max_tokens=_CHEAP_MAX_TOKENS,
        )
        try:
            result = json.loads(result_any) if isinstance(result_any, str) else result_any
        except (TypeError, json.JSONDecodeError):
            result = {"context_updates": [], "rationale": "tech_writer JSON parse failed"}

    story.tech_writer_result_json = json.dumps(result)

    # Apply context_updates to the app repo. In dry-run we use the factory's
    # apps/<app>/ as a stand-in — but apply_context_updates would write files
    # there. To keep dry-run truly dry (NO writes outside the factory state),
    # we DO NOT call apply_context_updates in dry-run. Instead we record the
    # updates that WOULD have been applied.
    #
    # IMPORTANT: We MUST do the write BEFORE advancing state to TECH_WRITER_DONE
    # so a failed apply leaves the chain in a recoverable place. If the writer
    # produced a forbidden path (or any other write failure), we transition to
    # REVIEWER_REQUESTED_CHANGES so the chain routes back through the dev loop
    # instead of pretending docs are current.
    if not dry_run:
        updates_raw = result.get("context_updates") or []
        updates = [
            ContextUpdate(
                path=u["path"], action=u.get("action", "rewrite"), content=u.get("content", "")
            )
            for u in updates_raw
        ]
        repo_path = software_factory_root / "apps" / story.app
        try:
            apply_context_updates(updates, repo_path)
        except Exception as exc:
            # Bounce back through reviewer_requested_changes: tech_writer
            # produced an invalid context update; either the writer is
            # confused (rare) or it tried to write to a forbidden path. The
            # next dev cycle gets a clean shot.
            story.state = advance(story, EVENT_REVIEWER_REQUEST_CHANGES).value
            story.error = f"context update failed: {exc}"
            persist_story(story, db)
            return HandlerResult(
                next_state=StoryState(story.state),
                payload=result,
                error=story.error,
            )

    story.state = advance(story, EVENT_TECH_WRITER_DONE).value
    persist_story(story, db)
    return HandlerResult(next_state=StoryState(story.state), payload=result)


# --------------------------------------------------------------------------- #
# docs_enforcer
# --------------------------------------------------------------------------- #


def handle_docs_enforcer(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    pr_files: list[str] | None = None,
    github_client: Any = None,
) -> HandlerResult:
    """Run the canonical-paths enforcer over the PR's file list.

    Violations → REVIEWER_REQUESTED_CHANGES + label + comment.
    Clean → PR_OPEN.

    In dry-run mode without an explicit ``pr_files`` list, we derive a
    plausible set from the tech_writer result (so the enforcer sees the
    same paths the tech_writer claimed to write).
    """
    db = db_path or (software_factory_root / "state" / "factory.db")
    story.state = advance(story, EVENT_DOCS_ENFORCER_CHECK).value
    persist_story(story, db)

    files = pr_files
    if files is None:
        # Derive from the tech_writer result, if present.
        files = []
        tw_raw = story.tech_writer_result_json or "{}"
        try:
            tw = json.loads(tw_raw)
            for u in tw.get("context_updates") or []:
                files.append(str(u.get("path")))
        except json.JSONDecodeError:
            pass

    violations = scan_pr_diff(files)
    payload: dict[str, Any] = {"violations": [v._asdict() for v in violations], "files": files}

    if violations:
        # Real-run: post the comment + label the PR.
        if not dry_run and github_client is not None and story.github_pr_number is not None:
            try:
                repo = github_client.get_repo(app_config.repo)
                pr = repo.get_pull(story.github_pr_number)
                pr.create_issue_comment(format_violation_comment(violations))
                pr.add_to_labels("canonical-paths-violation")
            except Exception:  # pragma: no cover - real-run path
                pass

        story.state = advance(story, EVENT_DOCS_ENFORCER_FAIL).value
        story.error = f"canonical-paths violations: {len(violations)}"
        persist_story(story, db)
        return HandlerResult(next_state=StoryState(story.state), payload=payload, error=story.error)

    story.state = advance(story, EVENT_DOCS_ENFORCER_PASS).value
    persist_story(story, db)
    return HandlerResult(next_state=StoryState(story.state), payload=payload)


# --------------------------------------------------------------------------- #
# helpers used by the orchestrator
# --------------------------------------------------------------------------- #


def find_direction_for_story(story: StoryRecord, software_factory_root: Path) -> Direction | None:
    """Look up the parsed Direction record for a story (by direction_id)."""
    for dpath in list_direction_dirs(story.app, software_factory_root):
        if dpath.name.startswith(f"{story.direction_id}-"):
            return parse_direction_dir(story.app, dpath)
    return None


# --------------------------------------------------------------------------- #
# Phase 5 — handle_deploy
# --------------------------------------------------------------------------- #


def handle_deploy(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    fixture_step_outputs: list[tuple[int, str, str]] | None = None,
    fixture_step_outputs_by_phase: dict[str, list[tuple[int, str, str]]] | None = None,
    github_client: Any = None,
) -> HandlerResult:
    """Drive the post-merge deploy for ``story``.

    Pre-conditions: story.state == DEPLOY_PENDING and
    ``story.github_pr_number`` is set. The handler delegates to the deploy
    orchestrator, then flips the story state to DEPLOYED on success,
    BLOCKED_DEPLOY_FAILED on hard failure, or DEPLOYED with skip marker
    when the app has ``deploy.enabled=false``.
    """
    from factory.chain.state_machine import (
        EVENT_DEPLOY_FAILED,
        EVENT_DEPLOY_SKIPPED,
        EVENT_DEPLOY_STARTED,
        EVENT_DEPLOY_SUCCEEDED,
    )
    from factory.deploy.orchestrator import deploy_post_merge

    db = db_path or (software_factory_root / "state" / "factory.db")
    if story.state != StoryState.DEPLOY_PENDING.value:
        return HandlerResult(
            next_state=StoryState(story.state),
            error=f"handle_deploy called from non-deploy_pending state {story.state!r}",
        )
    if story.github_pr_number is None:
        # No PR number means we can't deploy a specific SHA; treat as skip.
        story.state = advance(story, EVENT_DEPLOY_SKIPPED).value
        story.error = "deploy_skipped_no_pr_number"
        persist_story(story, db)
        return HandlerResult(next_state=StoryState(story.state), error=story.error)

    # If deploy is disabled, transition to DEPLOYED with skip marker. The
    # orchestrator's deploy_post_merge already records the action row with
    # status="skipped"; we just observe the result here and short-circuit
    # the state.
    if not app_config.deploy.enabled:
        story.state = advance(story, EVENT_DEPLOY_SKIPPED).value
        persist_story(story, db)
        return HandlerResult(
            next_state=StoryState(story.state),
            payload={"skipped": True, "reason": "deploy_disabled_in_config"},
        )

    # Mark started.
    story.state = advance(story, EVENT_DEPLOY_STARTED).value
    persist_story(story, db)

    # The merged_sha for a story without a recorded SHA falls back to a
    # placeholder so the orchestrator still runs (real-run flows wire the
    # SHA from the GH merge response on the webhook path; see
    # factory/webhook/github.py).
    merged_sha = "pending-sha"
    action = deploy_post_merge(
        story.app,
        story.github_pr_number,
        merged_sha,
        software_factory_root,
        dry_run=dry_run,
        fixture_step_outputs=fixture_step_outputs,
        fixture_step_outputs_by_phase=fixture_step_outputs_by_phase,
        github_client=github_client,
        db_path=db,
    )

    if action.success:
        story.state = advance(story, EVENT_DEPLOY_SUCCEEDED).value
        story.error = None
        persist_story(story, db)
        return HandlerResult(
            next_state=StoryState(story.state),
            payload={"deploy_action": True, "success": True},
        )

    # Failure (or skipped via pre-flight). If pre-flight skipped (e.g.
    # mode_blocks_deploy), route to DEPLOYED with the marker rather than
    # BLOCKED — the operator hasn't actually deployed yet but the story
    # isn't blocked by code quality.
    if action.error and action.error in {
        "mode_blocks_deploy",
        "deploy_disabled_in_config",
    }:
        story.state = advance(story, EVENT_DEPLOY_SKIPPED).value
        story.error = action.error
        persist_story(story, db)
        return HandlerResult(
            next_state=StoryState(story.state),
            payload={"skipped": True, "reason": action.error},
        )

    story.state = advance(story, EVENT_DEPLOY_FAILED).value
    story.error = action.error or "deploy_failed_unknown"
    persist_story(story, db)
    return HandlerResult(
        next_state=StoryState(story.state),
        payload={
            "deploy_action": True,
            "rolled_back": action.rolled_back,
            "p0_issue_number": action.p0_issue_number,
        },
        error=story.error,
    )
