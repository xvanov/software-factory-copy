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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, SQLModel, create_engine, select

from factory.app_config import AppConfig
from factory.chain.event_log import log_story_event
from factory.chain.state_machine import (
    EVENT_DEV_EXHAUSTED,
    EVENT_DEV_STARTED,
    EVENT_DEV_TESTS_GREEN,
    EVENT_DEV_TESTS_RED,
    EVENT_DOCS_ENFORCER_CHECK,
    EVENT_DOCS_ENFORCER_FAIL,
    EVENT_DOCS_ENFORCER_PASS,
    EVENT_DOCS_ONBOARDER_DONE,
    EVENT_DOCS_ONBOARDER_FAILED,
    EVENT_DOCS_ONBOARDER_STARTED,
    EVENT_DOCS_SM_DONE,
    EVENT_DOCS_SM_STARTED,
    EVENT_HARNESS_PRECHECK_FAIL,
    EVENT_HARNESS_PRECHECK_PASS,
    EVENT_HARNESS_PRECHECK_STARTED,
    EVENT_REVIEW_NONCONVERGENT,
    EVENT_REVIEWER_APPROVE,
    EVENT_REVIEWER_REQUEST_CHANGES,
    EVENT_REVIEWER_STARTED,
    EVENT_REVIEWER_TEST_QUALITY,
    EVENT_SM_DONE,
    EVENT_SM_STARTED,
    EVENT_TECH_WRITER_DONE,
    EVENT_TECH_WRITER_STARTED,
    EVENT_TEST_DESIGN_DONE,
    EVENT_TEST_DESIGN_STARTED,
    EVENT_TEST_IMPL_SLOP,
    EVENT_TEST_IMPL_SLOP_RETRY,
    EVENT_TEST_IMPL_STARTED,
    EVENT_TESTS_NEED_CLARIFICATION,
    EVENT_TESTS_RED,
    StoryRecord,
    StoryState,
    advance,
)
from factory.chain.worktree import ensure_worktree_for_story
from factory.context.enforcer import format_violation_comment, scan_pr_diff
from factory.context.updater import ContextUpdate, apply_context_updates
from factory.directions.parser import Direction, list_direction_dirs, parse_direction_dir
from factory.model_router import max_output_tokens_for, route

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
# Per-story worktree helper
# --------------------------------------------------------------------------- #


def _writing_worktree(
    app_config: AppConfig,
    software_factory_root: Path,
    story: StoryRecord,
) -> Path:
    """Resolve (and lazily create) the per-story worktree the chain writes
    into.

    All "writing" handlers — those that run a sandbox, commit, or push —
    must use this instead of ``resolve_app_repo_path``. The source repo
    at ``app_repo_path`` is the operator's checkout and is treated as
    read-only by the chain; each in-flight story gets its own private
    worktree under ``state/worktrees/`` so multiple sandboxes can run
    in parallel without racing on a shared working tree.

    Read-only callsites (context-prelude scans) keep using
    ``resolve_app_repo_path`` because they don't mutate state.
    """
    from factory.app_config import resolve_app_repo_path

    source_repo = resolve_app_repo_path(app_config, software_factory_root)
    return ensure_worktree_for_story(
        source_repo,
        software_factory_root=software_factory_root,
        app=story.app,
        story_id=story.github_issue_number,
        slug=story.slug,
        base_branch=app_config.default_branch or "main",
    )


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #


_MIGRATION_COLUMNS: dict[str, str] = {
    # column_name -> SQL type (idempotent ALTER TABLE ADD COLUMN if missing).
    "sm_result_json": "TEXT",
    "last_rejection_reason": "TEXT",
    # Docs-chain support. Default ``"tdd"`` keeps pre-existing stories on the
    # historical pipeline; new docs-scope stories opt in via PM's child_story
    # output (``chain_kind: "docs"``).
    "chain_kind": "TEXT NOT NULL DEFAULT 'tdd'",
    # JSONL of prior dev sandbox attempts (one entry per chain-level retry):
    # ``[{attempt, ts, test_output_tail, files_touched, summary}, ...]``.
    # Fed forward into the next dev invocation's initial message so the LLM
    # sees what it already tried and what failed.
    "dev_attempts_json": "TEXT",
    # Item 4 — harness precheck flag. Bool, default 0 (False). SQLite
    # stores bool as 0/1 INTEGER; the SQLModel layer coerces on read.
    "harness_precheck_passed": "INTEGER NOT NULL DEFAULT 0",
    # Hard convergence guard counter. INTEGER default 0; pre-existing stories
    # gain it on next visit and start counting from their next reviewer pass.
    "reviewer_cycles": "INTEGER NOT NULL DEFAULT 0",
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

    Phase 7 — dual-draft branch:
      * If ``should_spawn_dual_draft(direction, pm_result)`` returns True
        (``(explore)`` tag OR PM confidence < 0.6), the chain produces
        TWO interpretations and spawns two StoryRecords (one per
        interpretation) instead of consuming ``pm_result.child_stories``
        verbatim. Each story carries the ``alt-a`` / ``alt-b`` suffix in
        its slug + branch so downstream draft PRs are distinguishable.
    """
    from factory.chain.dual_draft import (
        link_alternatives,
        produce_interpretations,
        should_spawn_dual_draft,
    )

    db = db_path or (software_factory_root / "state" / "factory.db")
    out: list[StoryRecord] = []

    # Phase 7: dual-draft branch — spawn two interpretation stories,
    # regardless of how many child_stories the PM emitted.
    if should_spawn_dual_draft(direction, pm_result):
        interpretations = produce_interpretations(
            direction,
            pm_result,
            dry_run=dry_run,
        )
        # Pick scope from the first child_story if present; otherwise
        # default to ``backend`` (most common case for ambiguous asks).
        first_child = (pm_result.get("child_stories") or [{}])[0]
        scope = str(first_child.get("scope") or "backend")
        # ``chain_kind`` is inherited from the first child story since
        # dual-draft produces two interpretations of the same underlying
        # work item; the variant doesn't change per-interpretation.
        chain_kind = str(first_child.get("chain_kind") or "tdd")

        for interp in interpretations:
            slug_base = _slug_of(interp.title or direction.title or "story")
            # Force interpretation_id into the slug so two draft PRs
            # don't collide on branch names.
            slug = f"{slug_base}-{interp.interpretation_id}"[:60].strip("-") or "story"
            title = f"{interp.title}"[:200]
            issue_number: int | None = None
            story_file_path = f"stories/0-{slug}.md"
            if not dry_run and github_client is not None:
                repo = github_client.get_repo(app_config.repo)
                body = (
                    f"{interp.body}\n\n"
                    f"_Direction: `{direction.id}-{direction.slug}` "
                    f"(tracker: #{direction.state.get('tracker_issue', '?')})._\n"
                    f"\n_Interpretation: `{interp.interpretation_id}` — "
                    f"{interp.key_assumption_diff}_\n"
                )
                issue = repo.create_issue(
                    title=title,
                    body=body,
                    labels=["story", f"scope/{scope}", "draft-alternative"],
                )
                issue_number = int(issue.number)
                story_file_path = f"stories/{issue_number}-{slug}.md"

            # Dual-draft path: use the first child's points as a hint
            # (interpretations share the same underlying work item).
            dd_points_raw = first_child.get("points")
            try:
                dd_points = int(dd_points_raw) if dd_points_raw is not None else 3
            except (TypeError, ValueError):
                dd_points = 3
            from factory.observability.estimator import (
                estimate_story_seconds as _est_secs_dd,
            )

            dd_estimated_seconds = _est_secs_dd(
                db_path=db, points=dd_points, chain_kind=chain_kind
            )
            story = StoryRecord(
                direction_id=direction.id or direction.slug,
                app=direction.app,
                title=title,
                slug=slug,
                scope=scope,
                state=StoryState.STORY_CREATED.value,
                chain_kind=chain_kind,
                github_issue_number=issue_number,
                github_branch=f"story/{issue_number or 0}-{slug}",
                story_file_path=story_file_path,
                points=dd_points,
                estimated_seconds=dd_estimated_seconds,
            )
            persist_story(story, db)
            out.append(story)

        # Post the comparison comment on the tracker (real-run only).
        if not dry_run and github_client is not None and len(out) >= 2:
            try:
                link_alternatives(
                    out[0],
                    out[1],
                    interpretations,
                    direction,
                    github_client,
                    app_repo=app_config.repo,
                )
            except Exception:
                # Never fail the spawn on a comparison-comment hiccup.
                pass
        return out

    # EBS: compute baselines lazily so the very first story spawn after a
    # cold start still gets a usable ``estimated_seconds`` (None when no
    # samples yet — the Monte Carlo simulator gates on N>=5 to keep cold
    # starts from emitting bogus ETAs).
    from factory.observability.estimator import estimate_story_seconds

    child_stories = pm_result.get("child_stories") or []
    for child in child_stories:
        slug = _slug_of(child.get("title") or "story")
        title = str(child.get("title") or "Untitled story")[:200]
        scope = str(child.get("scope") or "backend")
        # ``chain_kind`` decides which chain variant drives this story.
        # PM emits ``"docs"`` for documentation-only deliverables (the new
        # docs chain) and ``"tdd"`` (default) for everything else.
        chain_kind = str(child.get("chain_kind") or "tdd")
        # EBS: Fibonacci difficulty points from PM; default to 3 (median).
        points_raw = child.get("points")
        try:
            points = int(points_raw) if points_raw is not None else 3
        except (TypeError, ValueError):
            points = 3
        estimated_seconds = estimate_story_seconds(
            db_path=db, points=points, chain_kind=chain_kind
        )
        issue_number = None
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
            chain_kind=chain_kind,
            github_issue_number=issue_number,
            github_branch=f"story/{issue_number or 0}-{slug}",
            story_file_path=story_file_path,
            points=points,
            estimated_seconds=estimated_seconds,
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
        from factory.app_config import resolve_app_repo_path
        from factory.context.loader import compose_context_prelude
        from factory.directions.parser import get_direction_chain
        from factory.runner import text_run

        persona = "sm"
        persona_prompt = _read_persona_prompt(persona)
        chain = (
            get_direction_chain(direction, software_factory_root)
            if direction is not None
            else None
        )
        prelude = compose_context_prelude(
            persona=persona,
            app_repo_path=resolve_app_repo_path(app_config, software_factory_root),
            task_scope=story.scope,
            direction_chain=chain,
            software_factory_root=software_factory_root,
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
            max_tokens=max_output_tokens_for(model_id),
            story_id=story.id,
            app=story.app,
            direction_id=story.direction_id,
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


# Cheap cap for personas that emit small structured updates (docs_sm,
# tech_writer). "Strong" personas (SM, Test-Designer, Test-Implementer,
# Dev, Reviewer) now resolve their cap per-model via
# ``max_output_tokens_for(model_id)``, which reads ``model_limits`` in
# routes.yaml — Claude 4.x gets 32k, GPT-5.4 gets 16k, DeepSeek-V4 gets
# 8k, etc. The previous single 8192 constant under-utilized every model
# except DeepSeek and the legacy 4096 truncated SM JSON mid-string on
# multi-story directions.
_CHEAP_MAX_TOKENS = 2048


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
        from factory.app_config import resolve_app_repo_path
        from factory.context.loader import compose_context_prelude
        from factory.directions.parser import get_direction_chain
        from factory.runner import text_run

        persona = "test_designer"
        persona_prompt = _read_persona_prompt(persona)
        direction = find_direction_for_story(story, software_factory_root)
        chain = (
            get_direction_chain(direction, software_factory_root)
            if direction is not None
            else None
        )
        prelude = compose_context_prelude(
            persona=persona,
            app_repo_path=resolve_app_repo_path(app_config, software_factory_root),
            task_scope=story.scope,
            direction_chain=chain,
            software_factory_root=software_factory_root,
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
            max_tokens=max_output_tokens_for(model_id),
            story_id=story.id,
            app=story.app,
            direction_id=story.direction_id,
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

        # Each in-flight story gets its own private git worktree under
        # ``state/worktrees/<app>-<id>-<slug>/`` so multiple sandboxes
        # can run in parallel without racing on a shared working tree.
        # The worktree is checked out on the per-story feature branch
        # automatically — no separate ``ensure_feature_branch`` call.
        target_repo = _writing_worktree(app_config, software_factory_root, story)
        repo_path = target_repo
        # Story file lives under the FACTORY tree (it's chain metadata), not in
        # the app repo. Compose its absolute path from the factory root.
        story_file_path_obj = software_factory_root / "apps" / story.app / story.story_file_path

        from factory.chain.branch import feature_branch_name

        branch = feature_branch_name(story.github_issue_number, story.slug)
        story.github_branch = branch
        persist_story(story, db)

        ti_direction = find_direction_for_story(story, software_factory_root)
        from factory.directions.parser import get_direction_chain
        ti_chain = (
            get_direction_chain(ti_direction, software_factory_root)
            if ti_direction is not None
            else None
        )

        llm = LLMConfig(model=route("test_implementer"))
        import asyncio

        # If test_implementer was re-dispatched because the reviewer rejected
        # on test quality (REVIEWER_IN_PROGRESS --reviewer_test_quality-->
        # TEST_DESIGN_DONE), hand it the reviewer's test findings so it knows
        # exactly which tests to rewrite instead of regenerating blindly.
        ti_reviewer_findings: dict[str, Any] | None = None
        if story.reviewer_result_json:
            try:
                parsed = json.loads(story.reviewer_result_json)
                if isinstance(parsed, dict) and (
                    parsed.get("test_quality_findings") or parsed.get("findings")
                ):
                    ti_reviewer_findings = parsed
            except (json.JSONDecodeError, TypeError):
                ti_reviewer_findings = None

        run_res = asyncio.run(
            sandbox_run(
                persona="test_implementer",
                story_path=story_file_path_obj,
                repo_path=repo_path,
                llm_config=llm,
                dry_run=False,
                direction_chain=ti_chain,
                software_factory_root=software_factory_root,
                test_command=app_config.gates.test_command,
                reviewer_findings=ti_reviewer_findings,
                story_id=story.id,
                app=story.app,
                direction_id=story.direction_id,
            )
        )

        # Commit whatever the sandbox left uncommitted. The
        # ``test_implementer.md`` persona prompt promises "the chain does the
        # actual git commit" — without this step the test files stay in the
        # working tree and the next handler (Dev) inherits them. ``git diff
        # HEAD~..HEAD`` then attributes the test files to Dev's commit, and
        # the "Dev modified test files" guard in ``handle_dev`` blocks the
        # story to BLOCKED_TESTS_NEED_CLARIFICATION. Onboarder uses the same
        # pattern — see ``handle_docs_onboarder``.
        from factory.chain.branch import _run_git

        try:
            _run_git(target_repo, "add", "-A")
            status_after_add = _run_git(
                target_repo, "status", "--porcelain"
            ).stdout.strip()
            if status_after_add:
                _run_git(
                    target_repo,
                    "commit",
                    "-m",
                    f"test: red tests for story {story.id} ({story.slug})\n\n"
                    f"Produced by Test-Implementer for direction "
                    f"{story.direction_id}.",
                )
        except Exception as exc:
            # A commit failure here is a chain bug, not a persona failure —
            # surface it as an error on the story so it shows up in
            # ``factory why`` instead of silently letting Dev inherit a
            # dirty tree.
            story.error = f"test_implementer commit failed: {exc}"
            persist_story(story, db)
            raise

        # The sandbox returned ok if tests are red (which is the desired outcome).
        result = {
            "files_written": run_res.files_changed,
            "test_command_run": app_config.gates.test_command
            or app_config.deploy.health_check_command
            or "(test_command)",
            "exit_code": 0 if run_res.test_run_passed else 1,
            "slop_detected": bool(run_res.test_run_passed),
            "output_excerpt": run_res.summary[-2000:],
            "summary": run_res.error or "test_implementer completed",
        }

    story.test_implementer_result_json = json.dumps(result)

    if result.get("slop_detected"):
        # Tests passed with NO implementation present → vacuous/slop tests.
        # This is a test-QUALITY problem, not env breakage: re-run the
        # Test-Implementer with explicit feedback so it rewrites the tests to
        # actually exercise the unbuilt behavior (red). Cap retries per the
        # "nothing loops >3" rule; only block for human attention at the cap.
        from factory.chain.event_log import read_story_events

        prior_slop_retries = sum(
            1
            for e in read_story_events(
                story.id, software_factory_root=software_factory_root, slug_hint=story.slug
            )
            if e.get("event") == "test_impl_slop_retry"
        )
        if prior_slop_retries < _MAX_TEST_IMPL_SLOP_RETRIES:
            # Feed the slop back through the SAME channel the reviewer
            # test-quality rejection uses: handle_test_implementation reads
            # ``story.reviewer_result_json`` for ``test_quality_findings`` and
            # _build_initial_message renders them as "rewrite the TESTS".
            story.reviewer_result_json = json.dumps(
                {
                    "summary": (
                        "Your tests PASSED before any implementation existed. They do "
                        "not exercise the new behavior — a correct TDD test must FAIL "
                        "(red) until the feature is built."
                    ),
                    "test_quality_findings": [
                        {
                            "test_name": "(all newly-written tests)",
                            "issue": (
                                "The suite passes with no production implementation "
                                "present, so it asserts nothing about the feature this "
                                "story introduces (vacuous / slop tests)."
                            ),
                            "fix_suggestion": (
                                "Rewrite the tests to import and assert on the specific "
                                "new API/behavior the story adds, so they fail now and "
                                "pass only once dev implements it."
                            ),
                        }
                    ],
                }
            )
            story.state = advance(story, EVENT_TEST_IMPL_SLOP_RETRY).value
            story.error = None
            persist_story(story, db)
            log_story_event(
                story.id,
                "test_impl_slop_retry",
                {"attempt": prior_slop_retries + 1, "cap": _MAX_TEST_IMPL_SLOP_RETRIES},
                software_factory_root=software_factory_root,
                slug_hint=story.slug,
            )
            return HandlerResult(next_state=StoryState(story.state), payload=result)

        # Cap hit — the test loop couldn't produce non-vacuous tests. Block for
        # human attention.
        story.state = advance(story, EVENT_TEST_IMPL_SLOP).value
        story.error = (
            f"tests passed pre-implementation (slop) after "
            f"{prior_slop_retries} test_implementer retries"
        )
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
# harness_precheck (Item 4)
# --------------------------------------------------------------------------- #


# pytest exit codes:
#   0  — all tests passed (we don't expect this pre-dev; tests SHOULD be red)
#   1  — tests collected and at least one failed (the desired pre-dev state)
#   2  — usage error / interrupted
#   3  — internal error
#   4  — pytest command-line usage error
#   5  — no tests collected
#
# 0 + 1 are "harness OK" (collection succeeded). 2/3/4/5 are
# "environmental failure" — dev cannot fix it; the chain must route to
# the operator-attention bucket with a clear redesign signal.
_HARNESS_PRECHECK_OK_EXIT_CODES = {0, 1}
_HARNESS_PRECHECK_TIMEOUT_S = 120

# Directories never treated as first-party source when resolving an import.
_NON_SOURCE_DIRS = {".venv", "venv", "site-packages", "node_modules", ".git", "__pycache__"}


def _first_party_package_exists(worktree_root: Path, top: str) -> bool:
    """True if ``top`` is a first-party Python package in the worktree.

    Checks the repo root and one level down (covers ``backend/app`` layouts)
    for a ``<top>/__init__.py``. Deliberately shallow: source packages live at
    the repo root or one directory in, never buried inside ``.venv``.
    """
    candidates = [worktree_root / top]
    try:
        for child in worktree_root.iterdir():
            if child.is_dir() and child.name not in _NON_SOURCE_DIRS:
                candidates.append(child / top)
    except OSError:
        return False
    return any((c / "__init__.py").exists() for c in candidates)


def _collection_failure_is_module_under_construction(output: str, worktree_root: Path) -> bool:
    """Classify a pytest collection failure (exit 2) as a legitimate TDD red.

    A brand-new-module story writes a test that imports the very module the
    story is meant to create (``from app.models.media_upload import ...``).
    At ``--collect-only`` time that import raises ``ModuleNotFoundError`` and
    pytest exits 2 — which the precheck would otherwise classify as
    environmental breakage and block. But this is the NORMAL red state for a
    new module: dev CAN fix it by creating the module.

    Returns True only when EVERY import error names a FIRST-PARTY module
    (its top-level package exists as source in the worktree). A missing
    third-party dependency (top package absent) or a broken ``conftest.py``
    is genuine environmental breakage → returns False → the story still
    blocks for operator attention.
    """
    # A conftest collection error is shared-infra breakage, never a single
    # story's module-under-construction. Block.
    if re.search(r"\bconftest\.py\b", output):
        return False
    missing = re.findall(r"ModuleNotFoundError: No module named ['\"]([\w.]+)['\"]", output)
    cannot_import = re.findall(
        r"ImportError: cannot import name ['\"][\w]+['\"] from ['\"]([\w.]+)['\"]",
        output,
    )
    candidates = missing + cannot_import
    if not candidates:
        # Exit 2 with no import error we recognise (SyntaxError, usage error,
        # etc.) — don't second-guess it; block.
        return False
    # Every reported failure must be a first-party module. One missing
    # third-party package is enough to keep the whole thing blocked.
    return all(_first_party_package_exists(worktree_root, mod.split(".")[0]) for mod in candidates)


def handle_harness_precheck(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    fixture_exit_code: int | None = None,
    fixture_output: str | None = None,
    fixture_worktree_root: Path | None = None,
) -> HandlerResult:
    """Run a one-shot test-collection check before dev gets dispatched.

    Why
    ---
    When pytest fails to *collect* (missing .env, ImportError in
    conftest, missing dep, wrong DATABASE_URL), dev sees a stack trace
    its code cannot fix and burns the entire retry budget on a config
    bug. This handler runs the configured ``test_command`` ONCE per
    story (gated by ``story.harness_precheck_passed``) inside the
    per-story worktree with ONLY the test files committed (i.e. before
    dev writes production code). If pytest can collect — exit 0 or 1 —
    the precheck passes and the orchestrator dispatches dev on the
    next iteration. If pytest blows up with a collection failure (exit
    2/3/4/5), the precheck routes the story to
    ``BLOCKED_TESTS_NEED_CLARIFICATION`` and emits a
    ``factory_needs_redesign`` event so the improver/operator sees the
    environmental gap.

    Dry-run / tests
    ---------------
    ``fixture_exit_code`` + ``fixture_output`` let tests drive the
    decision deterministically without spinning a real subprocess.
    """
    db = db_path or (software_factory_root / "state" / "factory.db")
    story.state = advance(story, EVENT_HARNESS_PRECHECK_STARTED).value
    persist_story(story, db)

    exit_code: int
    output: str
    worktree_root: Path | None = fixture_worktree_root

    if fixture_exit_code is not None:
        exit_code = fixture_exit_code
        output = fixture_output or "(fixture)"
    elif dry_run:
        # Dry-run with no fixture: assume the harness collects fine.
        # Tests that want to exercise the fail path always pass a
        # fixture exit code; the dry-run default is "happy harness".
        exit_code = 1  # collected, tests failed (desired pre-dev state)
        output = "(dry-run: harness assumed healthy)"
    else:
        # Real run: execute the app's test_command against the per-story
        # worktree. If no test_command is configured, treat as "harness
        # not declared, skip precheck cleanly" — same effect as a
        # successful precheck, since there's nothing to check.
        test_command = app_config.gates.test_command
        if not test_command:
            exit_code = 1
            output = "(no app_config.gates.test_command configured; precheck skipped)"
        else:
            # The precheck's job is to verify the harness can COLLECT the test
            # suite (catch ImportError / broken conftest / missing deps) BEFORE
            # dev runs — NOT to execute the suite. Executing the full command
            # makes the precheck run (and hang on) heavy/e2e tests, timing out
            # at _HARNESS_PRECHECK_TIMEOUT_S (exit 124) and wedging otherwise
            # healthy stories. For pytest, append --collect-only: it imports
            # every test module (surfacing the exact collection failures this
            # precheck is meant to catch, exit 2) in ~1s without executing any
            # test body. Non-pytest commands (e.g. playwright) run unchanged.
            precheck_command = test_command
            if "pytest" in test_command and "--collect-only" not in test_command:
                precheck_command = f"{test_command} --collect-only"
            target_repo = _writing_worktree(app_config, software_factory_root, story)
            worktree_root = target_repo
            try:
                import subprocess

                proc = subprocess.run(
                    precheck_command,
                    shell=True,
                    cwd=str(target_repo),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=_HARNESS_PRECHECK_TIMEOUT_S,
                )
                exit_code = proc.returncode
                output = (proc.stdout or "") + "\n" + (proc.stderr or "")
            except subprocess.TimeoutExpired as exc:
                exit_code = 124  # canonical timeout exit; not in OK set
                output = f"(precheck timed out after {_HARNESS_PRECHECK_TIMEOUT_S}s): {exc}"
            except Exception as exc:  # noqa: BLE001
                exit_code = 99
                output = f"(precheck subprocess raised): {exc!r}"

    passed = exit_code in _HARNESS_PRECHECK_OK_EXIT_CODES
    tail = output[-2000:]

    # Module-under-construction reclassification. A collection failure (exit 2)
    # caused solely by an ImportError for a FIRST-PARTY module the story is
    # meant to create is a legitimate TDD red — not environmental breakage dev
    # can't fix. Without this, every new-module story (new model, service,
    # package) blocks at precheck because its test imports a module that does
    # not exist yet. We let dev proceed; the missing module IS dev's job.
    under_construction = False
    if (
        not passed
        and exit_code == 2
        and worktree_root is not None
        and _collection_failure_is_module_under_construction(output, worktree_root)
    ):
        under_construction = True
        passed = True

    log_story_event(
        story.id,
        "harness_precheck",
        {
            "exit_code": exit_code,
            "passed": passed,
            "module_under_construction": under_construction,
            "output_tail": tail[-600:],
        },
        software_factory_root=software_factory_root,
        slug_hint=story.slug,
    )

    if passed:
        story.harness_precheck_passed = True
        story.state = advance(story, EVENT_HARNESS_PRECHECK_PASS).value
        persist_story(story, db)
        return HandlerResult(
            next_state=StoryState(story.state),
            payload={
                "passed": True,
                "exit_code": exit_code,
                "module_under_construction": under_construction,
                "output_tail": tail,
            },
        )

    # Precheck failed — the harness is broken. Route to
    # BLOCKED_TESTS_NEED_CLARIFICATION + emit factory_needs_redesign so
    # the improver picks up the signal.
    story.state = advance(story, EVENT_HARNESS_PRECHECK_FAIL).value
    story.error = (
        f"harness_precheck_failed: pytest exit_code={exit_code} "
        f"(expected 0 or 1; got something that means 'tests did not collect')"
    )
    persist_story(story, db)
    log_story_event(
        story.id,
        "factory_needs_redesign",
        {
            "kind": "harness_failure",
            "exit_code": exit_code,
            "output_tail": tail[-1200:],
            "suggestions": [
                "Test harness failed to collect — typical causes: missing "
                ".env in worktree, ImportError in conftest, missing "
                "dependency, wrong DATABASE_URL. Investigate the worktree "
                "environment before re-dispatching dev.",
            ],
            "branch": story.github_branch,
        },
        software_factory_root=software_factory_root,
        slug_hint=story.slug,
    )
    return HandlerResult(
        next_state=StoryState(story.state),
        payload={"passed": False, "exit_code": exit_code, "output_tail": tail},
        error=story.error,
    )


# --------------------------------------------------------------------------- #
# dev
# --------------------------------------------------------------------------- #


# Dev retry budget at the chain level. Each retry re-invokes the dev
# sandbox (which itself has ``max_iterations`` tool calls inside).
#
# 3 is deliberately low. The chain now feeds prior attempts' test-output
# tails forward into each retry's initial message, so "the LLM has no
# memory" is no longer the reason retries waste budget. Anything that
# can't be fixed in 3 informed attempts is a signal the factory itself
# needs work (test_implementer wrote impossible tests, persona prompt
# is wrong, context docs are missing key info, etc.) — not a problem
# more retries can fix. Exhaustion fires an explicit
# ``factory_needs_redesign`` event so the operator sees the signal.
# Bumped 3 -> 6 (operator-approved 2026-05-29): many stories were landing
# 1-N tests short of green and exhausting the budget; the extra informed
# attempts (dev now also receives the reviewer findings + the full prior-
# attempt history) materially improve convergence on the harder stories.
_MAX_DEV_RETRIES = 6

# Test-Implementer slop retry cap. When the Test-Implementer's tests pass
# BEFORE any implementation exists, they are vacuous (don't exercise the new
# behavior). That's a test-quality problem the test loop can fix, so we re-run
# the Test-Implementer with explicit slop feedback rather than terminally
# blocking — but capped at this many retries per the "nothing loops >3" rule.
# Counted from ``test_impl_slop_retry`` events in the per-story log.
_MAX_TEST_IMPL_SLOP_RETRIES = 3

# Hard convergence guard. A healthy story converges within a few review
# rounds; beyond this many request-changes verdicts the dev<->reviewer loop
# is judged non-converging and routed to BLOCKED_REVIEW_NONCONVERGENT instead
# of looping back to dev indefinitely. Counted by ``story.reviewer_cycles``.
_MAX_REVIEW_CYCLES = 3

# Pre-model sandbox infrastructure-error cap. A dev sandbox that fails BEFORE
# any model work (``success=False`` with zero tokens/cost and tests never run)
# is shared-infra breakage — a transient ``.venv`` relink from a concurrent
# ``uv run``/``uv sync`` re-materialising site-packages mid-render
# (``TemplateNotFound``), an SDK import failure, or a sandbox boot crash. This
# is NOT a dev-code or test failure, so it must NOT consume the dev retry
# budget; the story is bounced straight back to dev (most transients clear by
# the next tick). Counted from *consecutive trailing* ``dev_sandbox_infra_error``
# events so a recovered story is never punished for an earlier blip; at the cap
# a persistent infra fault escalates loudly via ``factory_needs_redesign``
# instead of looping forever. Capped at 3 per the "nothing loops >3" rule.
_MAX_DEV_SANDBOX_INFRA_RETRIES = 3

# Dev-made-no-progress test-repair cap. When a dev run produces ZERO file
# changes yet tests stay red, re-running dev is futile — unchanged code yields
# the identical failure. The dominant cause is a test-quality defect only the
# test_implementer can fix: contradictory assertions (e.g. the same nonexistent
# id asserted to return both 404 AND 501), impossible expectations, or tests
# that don't match the story contract. Instead of burning identical dev retries
# into a terminal block, route back to the test loop with dev's own diagnosis
# as the repair brief. Counted from `dev_nochange_test_repair` events; capped
# per the "nothing loops >3" rule, after which the story blocks with a SPECIFIC
# "tests unsatisfiable" signal rather than the generic dev-exhaustion.
_MAX_DEV_NOCHANGE_TEST_REPAIRS = 3


def _is_premodel_infra_failure(run_res: Any) -> bool:
    """True when a sandbox run failed before any model work began.

    A genuine "dev ran but tests are still red" outcome has
    ``test_run_passed is False`` and (almost always) non-zero token/cost
    usage. A pre-model infrastructure failure — the OpenHands SDK import
    blowing up, the agent prompt template failing to render, or the sandbox
    boot crashing — returns ``success=False`` with ``test_run_passed`` never
    set and zero tokens/cost. Distinguishing the two is what keeps an
    environment blip from masquerading as a code failure and burning the dev
    retry budget.
    """
    return bool(
        not run_res.success
        and run_res.test_run_passed is None
        and (run_res.cost_usd or 0.0) == 0.0
        and (run_res.tokens_out or 0) == 0
    )


def _consecutive_trailing_infra_errors(
    story: StoryRecord, software_factory_root: Path
) -> int:
    """Count ``dev_sandbox_infra_error`` events with no real dev progress after.

    Walks the per-story event log newest-first and counts the unbroken run of
    trailing infra-error events. Any genuine dev event (retry, exhaustion,
    green, clarification, or a fresh dev_started) resets the count — so a
    story that hit one transient blip, recovered, and only much later hits
    more is never blocked on a stale cumulative tally.
    """
    from factory.chain.event_log import read_story_events

    count = 0
    for e in reversed(
        read_story_events(
            story.id, software_factory_root=software_factory_root, slug_hint=story.slug
        )
    ):
        ev = e.get("event")
        if ev == "dev_sandbox_infra_error":
            count += 1
        elif ev in {
            "dev_retry",
            "dev_exhausted",
            "dev_tests_green",
            "tests_need_clarification",
        }:
            break
    return count


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
        from factory.chain.branch import (
            _run_git,
            feature_branch_name,
            find_test_files_in_diff,
        )
        from factory.runner import LLMConfig, sandbox_run

        # Dev runs in the same per-story worktree test_impl already created.
        # If test_impl wasn't run yet (e.g. retry path entered directly),
        # ``_writing_worktree`` will create it on demand.
        target_repo = _writing_worktree(app_config, software_factory_root, story)
        repo_path = target_repo
        story_file_path_obj = software_factory_root / "apps" / story.app / story.story_file_path
        difficulty = story.current_model_tier

        branch = feature_branch_name(story.github_issue_number, story.slug)
        story.github_branch = branch
        persist_story(story, db)

        # Snapshot the pre-dev tip so we can diff post-dev commits to enforce
        # the "Dev must not modify test files" invariant. The persona prompt
        # carries the rule for the LLM; this check is the chain-side
        # enforcement that catches violations regardless of what the model
        # decided to do.
        pre_dev_sha = _run_git(target_repo, "rev-parse", "HEAD").stdout.strip()

        dev_direction = find_direction_for_story(story, software_factory_root)
        from factory.directions.parser import get_direction_chain
        dev_chain = (
            get_direction_chain(dev_direction, software_factory_root)
            if dev_direction is not None
            else None
        )

        llm = LLMConfig(model=route("dev", difficulty=difficulty))
        import asyncio

        # Carry prior attempts forward — the LLM gets to see what was tried
        # and which assertions are still red, so retry N doesn't re-discover
        # the same dead ends retries 1..N-1 already hit.
        prior_attempts: list[dict[str, Any]] = []
        if story.dev_attempts_json:
            try:
                prior_attempts = json.loads(story.dev_attempts_json) or []
            except (json.JSONDecodeError, TypeError):
                prior_attempts = []

        # When dev is re-dispatched after the reviewer requested changes, hand
        # it the reviewer's actual findings. Tests are already green on this
        # path, so without the findings the dev LLM has no signal about what to
        # fix and the dev<->reviewer loop cannot converge. Only the most recent
        # reviewer verdict is relevant.
        reviewer_findings: dict[str, Any] | None = None
        if story.reviewer_result_json:
            try:
                parsed = json.loads(story.reviewer_result_json)
                if isinstance(parsed, dict) and (
                    parsed.get("findings") or parsed.get("test_quality_findings")
                ):
                    reviewer_findings = parsed
            except (json.JSONDecodeError, TypeError):
                reviewer_findings = None

        run_res = asyncio.run(
            sandbox_run(
                persona="dev",
                story_path=story_file_path_obj,
                repo_path=repo_path,
                llm_config=llm,
                difficulty=difficulty,
                dry_run=False,
                direction_chain=dev_chain,
                software_factory_root=software_factory_root,
                test_command=app_config.gates.test_command,
                prior_attempts=prior_attempts,
                reviewer_findings=reviewer_findings,
                story_id=story.id,
                app=story.app,
                direction_id=story.direction_id,
            )
        )

        # Pre-model sandbox infrastructure guard. If the sandbox died before
        # any model work (transient .venv relink → TemplateNotFound, SDK
        # import failure, boot crash), this is shared-infra breakage, NOT a
        # dev-code failure. Treating it as "tests red" below would burn a dev
        # retry on a problem dev cannot fix and produce a $0/0.2s retry storm.
        # Instead: do NOT count it as a dev retry, do NOT escalate the model
        # tier; bounce straight back to dev (the transient usually clears by
        # the next tick), bounded by a consecutive-error cap so a persistent
        # fault escalates loudly rather than looping.
        if not dry_run and _is_premodel_infra_failure(run_res):
            prior_infra = _consecutive_trailing_infra_errors(
                story, software_factory_root
            )
            infra_err = (run_res.error or "sandbox failed before model work")[:300]
            if prior_infra < _MAX_DEV_SANDBOX_INFRA_RETRIES:
                story.state = advance(story, EVENT_DEV_TESTS_RED).value
                story.error = None
                persist_story(story, db)
                log_story_event(
                    story.id,
                    "dev_sandbox_infra_error",
                    {
                        "attempt": prior_infra + 1,
                        "cap": _MAX_DEV_SANDBOX_INFRA_RETRIES,
                        "error": infra_err,
                        "dev_retries_preserved": story.dev_retries,
                    },
                    software_factory_root=software_factory_root,
                    slug_hint=story.slug,
                )
                return HandlerResult(
                    next_state=StoryState(story.state),
                    payload={
                        "test_run_passed": False,
                        "sandbox_infra_error": infra_err,
                        "summary": (
                            "Sandbox infrastructure failure before any model "
                            f"work: {infra_err[:200]}. Re-dispatching dev "
                            f"(infra retry {prior_infra + 1}/"
                            f"{_MAX_DEV_SANDBOX_INFRA_RETRIES}); dev retry "
                            "budget untouched."
                        ),
                    },
                    error=None,
                )
            # Cap hit — a persistent sandbox/infra fault dev cannot fix. Block
            # loudly so the operator/FMS sees genuine infra breakage rather
            # than a story silently consuming retries.
            story.state = advance(story, EVENT_DEV_EXHAUSTED).value
            story.error = (
                f"sandbox infrastructure failure persisted across "
                f"{_MAX_DEV_SANDBOX_INFRA_RETRIES} consecutive retries: {infra_err[:200]}"
            )
            persist_story(story, db)
            log_story_event(
                story.id,
                "factory_needs_redesign",
                {
                    "kind": "sandbox_infra_persistent",
                    "infra_retries": prior_infra,
                    "error": infra_err,
                    "suggestions": [
                        "The dev sandbox failed before any model work on every "
                        "attempt (e.g. TemplateNotFound / SDK import / boot "
                        "crash). This is environment breakage, not a code or "
                        "test problem dev can fix. Check for a concurrent "
                        "`uv run`/`uv sync` relinking the shared .venv, a "
                        "corrupt OpenHands install, or a missing prompt template.",
                    ],
                },
                software_factory_root=software_factory_root,
                slug_hint=story.slug,
            )
            return HandlerResult(
                next_state=StoryState(story.state),
                payload={
                    "test_run_passed": False,
                    "sandbox_infra_error": infra_err,
                },
                error=story.error,
            )

        # If Dev touched any test file, abort to BLOCKED_TESTS_NEED_CLARIFICATION.
        # Test files are frozen during the Dev run; if a test is wrong, the
        # persona prompt requires writing ``TESTS_NEED_CLARIFICATION:`` so the
        # chain can route back to Test-Designer. A silently-modified test that
        # then "passes" would be the worst possible regression.
        touched_tests = find_test_files_in_diff(target_repo, base_ref=pre_dev_sha)
        if touched_tests:
            story.state = advance(story, EVENT_DEV_EXHAUSTED).value
            story.error = f"dev modified test files (forbidden); paths={touched_tests[:5]}"
            persist_story(story, db)
            return HandlerResult(
                next_state=StoryState(story.state),
                payload={
                    "files_changed": run_res.files_changed,
                    "test_run_passed": False,
                    "tests_modified_by_dev": touched_tests,
                    "summary": (
                        f"Dev modified test files: {', '.join(touched_tests[:5])}. "
                        "Test files are frozen during dev runs; routing back to "
                        "Test-Designer for clarification."
                    ),
                },
                error=story.error,
            )

        tests_green = bool(run_res.test_run_passed)
        payload = {
            "files_changed": run_res.files_changed,
            "test_run_passed": tests_green,
            "summary": run_res.summary[-2000:],
        }

        # Dev's escape hatch when the test_implementer wrote impossible /
        # contradictory tests: emit ``TESTS_NEED_CLARIFICATION:`` and the
        # chain routes back to the test_designer/test_implementer flow
        # instead of burning more dev retries. The token is documented in
        # ``factory/personas/dev.md`` — this is the chain-side wiring that
        # actually catches it.
        if not tests_green and "TESTS_NEED_CLARIFICATION:" in (run_res.summary or ""):
            tail = run_res.summary or ""
            idx = tail.find("TESTS_NEED_CLARIFICATION:")
            clarification = tail[idx : idx + 800].splitlines()[0]
            # Route back to test_implementer via TEST_DESIGN_DONE — the next
            # tick dispatches handle_test_implementation which re-writes the
            # tests. dev_retries is NOT bumped so the budget is preserved
            # for the post-clarification dev run.
            story.state = advance(story, EVENT_TESTS_NEED_CLARIFICATION).value
            story.last_rejection_reason = f"tests_need_clarification: {clarification[:150]}"
            payload["tests_need_clarification"] = clarification
            persist_story(story, db)
            log_story_event(
                story.id,
                "tests_need_clarification",
                {"clarification": clarification[:300], "attempt": story.dev_retries + 1},
                software_factory_root=software_factory_root,
                slug_hint=story.slug,
            )
            return HandlerResult(
                next_state=StoryState(story.state),
                payload=payload,
                error=story.error,
            )

        # Objective "dev cannot fix this with code" signal: the run produced
        # NO file changes yet tests are still red. Re-dispatching dev is
        # pointless — unchanged code reproduces the identical failure (this is
        # exactly the no-op retry storm that marched stories 16/18/20/25/26
        # into terminal blocks). The overwhelmingly common cause is a
        # test-quality defect the test_implementer must repair (contradictory
        # / impossible / contract-mismatched tests); dev's SELF_SUMMARY usually
        # says so outright, but the literal TESTS_NEED_CLARIFICATION: token
        # above is too brittle to depend on. Route back to the test loop with
        # dev's diagnosis as the repair brief, capped, WITHOUT consuming the
        # dev retry budget.
        if not tests_green and not (run_res.files_changed or []):
            from factory.chain.event_log import read_story_events

            prior_repairs = sum(
                1
                for e in read_story_events(
                    story.id,
                    software_factory_root=software_factory_root,
                    slug_hint=story.slug,
                )
                if e.get("event") == "dev_nochange_test_repair"
            )
            diagnosis = (
                (getattr(run_res, "self_summary", "") or "")
                or (getattr(run_res, "last_assistant_message", "") or "")
                or (run_res.summary or "")
                or "Dev made no code changes and the tests stayed red."
            )
            if prior_repairs < _MAX_DEV_NOCHANGE_TEST_REPAIRS:
                # Same channel the slop path uses: handle_test_implementation
                # reads ``reviewer_result_json.test_quality_findings`` and
                # rewrites the failing tests with this brief instead of
                # regenerating blindly.
                story.reviewer_result_json = json.dumps(
                    {
                        "summary": (
                            "Dev made NO code changes this run and the tests "
                            "stayed red — a strong signal the TESTS are wrong "
                            "(contradictory / impossible / mismatched to the "
                            "story contract), not the implementation. Rewrite "
                            "the failing tests so they are mutually consistent "
                            "and satisfiable by a correct implementation."
                        ),
                        "test_quality_findings": [
                            {
                                "test_name": "(dev-reported test-quality defect)",
                                "issue": diagnosis[:1200],
                                "fix_suggestion": (
                                    "Ensure a single input maps to a single "
                                    "expected outcome and no two tests assert "
                                    "contradictory results for the same request."
                                ),
                            }
                        ],
                    }
                )
                story.state = advance(story, EVENT_TESTS_NEED_CLARIFICATION).value
                story.last_rejection_reason = (
                    f"dev_nochange_test_repair: {diagnosis[:150]}"
                )
                persist_story(story, db)
                log_story_event(
                    story.id,
                    "dev_nochange_test_repair",
                    {
                        "attempt": prior_repairs + 1,
                        "cap": _MAX_DEV_NOCHANGE_TEST_REPAIRS,
                        "diagnosis": diagnosis[:300],
                    },
                    software_factory_root=software_factory_root,
                    slug_hint=story.slug,
                )
                payload["tests_need_clarification"] = diagnosis[:300]
                return HandlerResult(
                    next_state=StoryState(story.state),
                    payload=payload,
                    error=story.error,
                )
            # Cap hit — repairing the tests didn't converge. The story's
            # contract is likely unsatisfiable as written; block with a
            # SPECIFIC signal so the operator/FMS sees the real cause rather
            # than a generic dev-exhaustion.
            story.state = advance(story, EVENT_DEV_EXHAUSTED).value
            story.error = (
                f"dev made no code changes across {_MAX_DEV_NOCHANGE_TEST_REPAIRS} "
                f"test-repair cycles; tests appear contradictory/unsatisfiable: "
                f"{diagnosis[:200]}"
            )
            persist_story(story, db)
            log_story_event(
                story.id,
                "factory_needs_redesign",
                {
                    "kind": "tests_unsatisfiable_no_dev_progress",
                    "repairs_attempted": prior_repairs,
                    "diagnosis": diagnosis[:400],
                    "suggestions": [
                        "Dev produced zero code changes across multiple "
                        "test-repair cycles — the tests are contradictory or "
                        "the story's contract is unsatisfiable as written. "
                        "Revisit the acceptance criteria and the "
                        "test_implementer output together; more dev retries "
                        "cannot fix this.",
                    ],
                },
                software_factory_root=software_factory_root,
                slug_hint=story.slug,
            )
            return HandlerResult(
                next_state=StoryState(story.state),
                payload=payload,
                error=story.error,
            )

    if tests_green:
        story.state = advance(story, EVENT_DEV_TESTS_GREEN).value
        persist_story(story, db)
        return HandlerResult(next_state=StoryState(story.state), payload=payload)

    # Not green — bump retries AND record this attempt's diagnostic so the
    # next retry sees what was tried and what failed. Dry-run has no
    # ``run_res`` (synthetic payload only); skip the feed-forward record
    # so the retry test fixture isn't tangled.
    story.dev_retries += 1
    if not dry_run:
        # Capture rich cross-retry memory: the file diff + test tail are
        # the "what failed" signal; ``self_summary`` + ``last_assistant_message``
        # + ``recent_tool_calls`` are the "what dev was thinking" signal.
        # The next retry's _build_initial_message renders both layers into
        # the prompt so the new conversation inherits the prior session's
        # context, not just its stack trace.
        attempt_record = {
            "attempt": story.dev_retries,
            "ts": datetime.now(UTC).isoformat(),
            "files_touched": (run_res.files_changed or [])[:20],
            "test_output_tail": (run_res.summary or "")[-1800:],
            "summary": (run_res.error or "tests not green after run")[:300],
            "self_summary": (getattr(run_res, "self_summary", "") or "")[:2000],
            "last_assistant_message": (
                getattr(run_res, "last_assistant_message", "") or ""
            )[:2000],
            "recent_tool_calls": list(getattr(run_res, "recent_tool_calls", []) or [])[
                -8:
            ],
        }
        try:
            prior = json.loads(story.dev_attempts_json or "[]")
            if not isinstance(prior, list):
                prior = []
        except (json.JSONDecodeError, TypeError):
            prior = []
        prior.append(attempt_record)
        # Cap history to last 5 entries — beyond that the prompt bloat outweighs
        # the signal, and we have the full chain in the per-story event log.
        story.dev_attempts_json = json.dumps(prior[-5:])
    if story.dev_retries >= _MAX_DEV_RETRIES:
        # Preserve whatever dev produced so the work doesn't evaporate
        # into a stash when the next story takes the working tree. Commit
        # any uncommitted changes, push the branch to origin, and surface
        # the failure with enough detail for a human (or follow-up persona
        # run) to pick it up without forensic stash-archaeology. The
        # story still terminates in BLOCKED_TESTS_NEED_CLARIFICATION but
        # the branch exists at origin and any partial code is committed.
        #
        # Dry-run skips the commit/push entirely — there's no real work
        # to preserve and ``_writing_worktree`` would try to ``git
        # worktree add`` against a fixture repo that doesn't exist.
        commit_pushed = False
        commit_sha: str | None = None
        if not dry_run:
            from factory.chain.branch import _run_git as _git

            target_repo = _writing_worktree(app_config, software_factory_root, story)
            try:
                _git(target_repo, "add", "-A")
                dirty = _git(target_repo, "status", "--porcelain").stdout.strip()
                if dirty:
                    _git(
                        target_repo,
                        "commit",
                        "-m",
                        (
                            f"wip(dev-exhausted): preserve partial work for story "
                            f"{story.id} ({story.slug})\n\n"
                            f"Dev exhausted {story.dev_retries} chain-level retries "
                            f"without reaching green. The chain commits this WIP so "
                            f"the work is recoverable from origin rather than living "
                            f"in a local stash. See ``factory why {story.id}`` for "
                            f"the per-attempt event log."
                        ),
                    )
                head_proc = _git(target_repo, "rev-parse", "HEAD")
                commit_sha = head_proc.stdout.strip() or None
                push_proc = _git(
                    target_repo,
                    "push",
                    "-u",
                    "origin",
                    story.github_branch or "HEAD",
                    check=False,
                )
                commit_pushed = push_proc.returncode == 0
                # Emit git signals — best-effort.
                try:
                    from factory.manager.signals import write_git_event as _wge

                    if dirty:
                        _wge(
                            kind="commit",
                            story_id=story.id,
                            worktree_path=str(target_repo),
                            commit_sha=commit_sha,
                            result="ok",
                            software_factory_root=software_factory_root,
                        )
                    _wge(
                        kind="push",
                        story_id=story.id,
                        worktree_path=str(target_repo),
                        result="ok" if commit_pushed else "error",
                        error=None if commit_pushed else "push failed",
                        software_factory_root=software_factory_root,
                    )
                except Exception:  # noqa: BLE001
                    pass
            except Exception as commit_exc:
                payload["dev_exhausted_commit_error"] = repr(commit_exc)

        story.state = advance(story, EVENT_DEV_EXHAUSTED).value
        story.error = (
            f"dev exhausted retries ({story.dev_retries}); "
            f"partial work {'pushed to origin' if commit_pushed else 'committed locally'}"
            f"{f' as {commit_sha[:12]}' if commit_sha else ''}"
        )
        payload["dev_exhausted_commit_sha"] = commit_sha
        payload["dev_exhausted_pushed"] = commit_pushed
        persist_story(story, db)
        log_story_event(
            story.id,
            "dev_exhausted",
            {
                "retries": story.dev_retries,
                "commit_sha": commit_sha,
                "pushed": commit_pushed,
                "files_changed": payload.get("files_changed", []),
            },
            software_factory_root=software_factory_root,
            slug_hint=story.slug,
        )
        # Exhausted retries with the new low cap + prior-attempts feed-forward
        # is a strong signal something upstream of dev is wrong: tests are
        # impossible, persona prompts need work, context docs lack
        # information, or PM cut the story too broadly. Emit a structured
        # "factory_needs_redesign" event with a recommendation so the
        # operator (or a future improver persona) can act on it instead of
        # just retrying more.
        prior_attempts_summary = []
        try:
            prior_attempts_summary = json.loads(story.dev_attempts_json or "[]")
        except (json.JSONDecodeError, TypeError):
            pass

        # Heuristic suggestions based on what the diagnostic actually shows.
        last_tail = ""
        if prior_attempts_summary:
            last_tail = prior_attempts_summary[-1].get("test_output_tail", "")
        suggestions: list[str] = []
        if "ImportError" in last_tail or "ModuleNotFoundError" in last_tail:
            suggestions.append(
                "Test harness fails at import — check .env / DATABASE_URL "
                "replication into the worktree, conftest dependencies, or "
                "missing fixtures. Not a code issue dev can fix."
            )
        if "TESTS_NEED_CLARIFICATION" in last_tail:
            suggestions.append(
                "Dev tried to escalate test-design — verify the escape "
                "hatch wiring is firing and test_implementer re-ran with "
                "the clarification."
            )
        same_tails = (
            len(prior_attempts_summary) >= 2
            and len({a.get("test_output_tail", "")[-300:] for a in prior_attempts_summary}) == 1
        )
        if same_tails:
            suggestions.append(
                "All attempts hit the exact same failure tail — dev's LLM "
                "is stuck on a problem code alone cannot fix. Re-prompt "
                "test_implementer or revisit the story's acceptance criteria."
            )
        if not suggestions:
            suggestions.append(
                "Investigate the per-story event log (``factory trace "
                f"{story.id}``) and the partial commit on origin "
                f"({commit_sha[:12] if commit_sha else 'no commit'}) — "
                "the chain ran out of attempts but the cause isn't obvious "
                "from the diagnostic."
            )

        log_story_event(
            story.id,
            "factory_needs_redesign",
            {
                "retries_used": story.dev_retries,
                "max_retries": _MAX_DEV_RETRIES,
                "last_test_output_tail": last_tail[-600:],
                "suggestions": suggestions,
                "branch": story.github_branch,
                "commit_sha": commit_sha,
            },
            software_factory_root=software_factory_root,
            slug_hint=story.slug,
        )

        # Post a comment on the direction's tracker issue (best-effort) so
        # the signal is loud + persistent + linkable.
        if not dry_run:
            try:
                _post_factory_needs_redesign_comment(
                    story=story,
                    app_config=app_config,
                    suggestions=suggestions,
                    last_tail=last_tail,
                    software_factory_root=software_factory_root,
                )
            except Exception as exc:  # never let a comment failure mask the real return
                log_story_event(
                    story.id,
                    "factory_needs_redesign_comment_failed",
                    {"error": repr(exc)},
                    software_factory_root=software_factory_root,
                    slug_hint=story.slug,
                )

        return HandlerResult(next_state=StoryState(story.state), payload=payload, error=story.error)

    # Escalate model tier on retry (standard -> hard).
    if story.current_model_tier == "standard":
        story.current_model_tier = "hard"
    story.state = advance(story, EVENT_DEV_TESTS_RED).value
    persist_story(story, db)
    log_story_event(
        story.id,
        "dev_retry",
        {
            "retries": story.dev_retries,
            "model_tier": story.current_model_tier,
            "files_changed": payload.get("files_changed", []),
            "summary_tail": payload.get("summary", "")[-500:],
        },
        software_factory_root=software_factory_root,
    )

    # Each retry IS a factory-improvement signal: under a healthy chain,
    # most stories should land first-pass. A retry means something upstream
    # of dev needs work — test_implementer wrote ambiguous tests, the persona
    # prompts missed context, the harness was subtly off, etc. Emit a
    # ``factory_needs_redesign`` event on EACH retry so the factory_improver
    # persona sees the pattern early instead of waiting for exhaustion.
    # ``kind: dev_retry_observed`` distinguishes this from the exhaustion
    # event the improver also consumes.
    if not dry_run:
        log_story_event(
            story.id,
            "factory_needs_redesign",
            {
                "kind": "dev_retry_observed",
                "retries": story.dev_retries,
                "max_retries": _MAX_DEV_RETRIES,
                "model_tier": story.current_model_tier,
                "last_test_output_tail": (payload.get("summary") or "")[-600:],
                "files_changed": payload.get("files_changed", [])[:10],
                "branch": story.github_branch,
                "suggestions": [
                    "Inspect this story's dev retry — even a single retry is a "
                    "failure signal worth aggregating. Recurring retries on the "
                    "same persona / scope shape mean a prompt or harness gap."
                ],
            },
            software_factory_root=software_factory_root,
            slug_hint=story.slug,
        )

    return HandlerResult(next_state=StoryState(story.state), payload=payload)


# --------------------------------------------------------------------------- #
# review
# --------------------------------------------------------------------------- #


# Section caps for handle_review / handle_tech_writer prompt plumbing. Keep
# these as module-level constants so the contract tests can import them.
# Section caps for the assembled review/tech_writer prompts. These are
# CHARACTER counts (str.__len__), not byte counts — Python's slicing
# operates on characters, and ``len(s) <= N`` uses the same unit. The
# ``_BYTES`` suffix is preserved for historical naming compatibility
# (and because for ASCII-heavy persona output the two coincide); a future
# refactor may rename them but the semantics are documented here so the
# constant is not ambiguous.
_STORY_CONTENT_CAP_BYTES = 32 * 1024  # characters; ~32KiB ASCII
_TEST_OUTPUT_CAP_BYTES = 8 * 1024  # characters; ~8KiB ASCII
_PR_DIFF_CAP_BYTES = 64 * 1024  # characters; ~64KiB ASCII

# Markers that indicate a prompt section was NOT populated with real data
# (the literal placeholder strings that caused stories 5/15/16/18/19/22 to
# cycle dev<->reviewer 5+ times). The sanity guard in handle_review raises
# RuntimeError if any of these survive into the final prompt; the prompt
# logger in factory.runner.text_run also scans for them so the watcher can
# surface regressions before they burn another month of reviewer cycles.
_BROKEN_PROMPT_MARKERS: tuple[str, ...] = (
    "(fetched from GitHub by the chain",
    "placeholder for real-run",
    # The literal "(see {" never appears when the f-string interpolates a
    # real path — only when the f-string itself was removed and replaced
    # by a bare string. We match the literal substring so a regressing
    # author swapping the f-string out is caught.
    "(see {",
)


def _read_story_file_content(
    story: StoryRecord, software_factory_root: Path
) -> str:
    """Return the story markdown content, capped, with a clear error fallback.

    The reviewer / tech_writer prompts used to embed ``(see <path>)`` instead
    of the actual file content — that meant the LLM was guessing about the
    story's acceptance criteria and routinely demanded clarifications about
    information that was already on disk.
    """
    if not story.story_file_path:
        return "(no story_file_path on record)"
    story_path = software_factory_root / "apps" / story.app / story.story_file_path
    try:
        content = story_path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError) as exc:
        return f"(story file unreadable at {story_path}: {exc!r})"
    if len(content) > _STORY_CONTENT_CAP_BYTES:
        content = content[:_STORY_CONTENT_CAP_BYTES] + "\n...[truncated at 32KB]"
    return content


def _fetch_latest_test_output(
    story: StoryRecord,
    software_factory_root: Path,
) -> str:
    """Return the most recent test output for ``story``.

    Preference order (richest signal first):

    1. The last entry of ``story.dev_attempts_json`` — dev writes a
       ``test_output_tail`` (1800 char tail) here on every red retry. This
       is the freshest signal when dev has run at least once.
    2. The most recent ``harness_precheck`` event in the per-story log —
       captures the output_tail of the one-shot pytest collect+exit that
       the chain runs before dev. Useful when the story bounced back to
       reviewer before dev produced an attempt of its own (e.g. tech_writer
       failure routed to REVIEWER_REQUESTED_CHANGES).
    3. ``"(no recent test run on record)"`` — explicit signal to the
       reviewer that test output isn't available, instead of an empty
       JSON object that looks indistinguishable from a passing run.

    Output is capped at 8KB regardless of source.
    """
    # 1. dev_attempts_json (preferred)
    if story.dev_attempts_json:
        try:
            attempts = json.loads(story.dev_attempts_json)
        except (TypeError, json.JSONDecodeError):
            attempts = None
        if isinstance(attempts, list) and attempts:
            last = attempts[-1]
            tail = (last.get("test_output_tail") or "").strip()
            if tail:
                header = (
                    f"(from dev_attempts[-1]; "
                    f"attempt={last.get('attempt')!r} "
                    f"ts={last.get('ts')!r})\n"
                )
                body = header + tail
                if len(body) > _TEST_OUTPUT_CAP_BYTES:
                    body = body[:_TEST_OUTPUT_CAP_BYTES] + "\n...[truncated at 8KB]"
                return body

    # 2. harness_precheck event log
    if story.id is not None:
        try:
            from factory.chain.event_log import read_story_events

            events = read_story_events(
                story.id,
                software_factory_root=software_factory_root,
                slug_hint=story.slug,
            )
        except Exception:  # noqa: BLE001 — log read must never break review
            events = []
        for ev in reversed(events):
            if ev.get("event") == "harness_precheck" and ev.get("output_tail"):
                tail = str(ev.get("output_tail")).strip()
                header = (
                    f"(from harness_precheck event; "
                    f"exit_code={ev.get('exit_code')!r} "
                    f"passed={ev.get('passed')!r} "
                    f"ts={ev.get('ts')!r})\n"
                )
                body = header + tail
                if len(body) > _TEST_OUTPUT_CAP_BYTES:
                    body = body[:_TEST_OUTPUT_CAP_BYTES] + "\n...[truncated at 8KB]"
                return body

    return "(no recent test run on record)"


def _fetch_pr_diff_for_review(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
) -> str:
    """Return the diff the reviewer should look at.

    Two cases:

    * ``story.github_pr_number`` is set → ``gh pr diff <num> -R <repo>``.
      This is the source of truth once a PR exists.
    * Otherwise → ``git diff origin/<default_branch>...HEAD`` inside the
      per-story worktree. The chain creates a PR lazily; reviewer can fire
      before that point, and we still want a real diff, not a placeholder.

    Either way we cap the result at 64KB. Subprocess failures are swallowed
    and surfaced inside the returned string (prefixed with ``"(...)"``)
    so the reviewer sees the cause instead of an empty section. The
    BROKEN_PROMPT_MARKERS guard further down catches the case where this
    helper itself regresses and returns a literal placeholder.
    """
    import subprocess

    diff_text: str

    pr_number = story.github_pr_number
    if pr_number is not None:
        try:
            proc = subprocess.run(
                ["gh", "pr", "diff", str(pr_number), "-R", app_config.repo],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            return f"(gh pr diff failed: {exc!r})"
        if proc.returncode != 0:
            diff_text = (
                f"(gh pr diff #{pr_number} returned rc={proc.returncode}; "
                f"stderr_tail={proc.stderr.strip()[-200:]!r})"
            )
        else:
            diff_text = proc.stdout or ""
    else:
        # No PR yet — diff the worktree against the default branch.
        try:
            worktree = _writing_worktree(app_config, software_factory_root, story)
        except Exception as exc:  # noqa: BLE001
            return f"(could not resolve writing worktree: {exc!r})"
        base = app_config.default_branch or "main"
        try:
            proc = subprocess.run(
                ["git", "diff", f"origin/{base}...HEAD"],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            return f"(git diff worktree failed: {exc!r})"
        if proc.returncode != 0:
            diff_text = (
                f"(git diff origin/{base}...HEAD returned rc={proc.returncode}; "
                f"stderr_tail={proc.stderr.strip()[-200:]!r})"
            )
        else:
            diff_text = proc.stdout or ""

    if not diff_text.strip():
        return "(diff is empty — no commits on this branch beyond the base)"
    if len(diff_text) > _PR_DIFF_CAP_BYTES:
        diff_text = diff_text[:_PR_DIFF_CAP_BYTES] + "\n...[truncated at 64KB]"
    return diff_text


def _assert_no_broken_prompt_markers(full_prompt: str, *, where: str) -> None:
    """Sanity guard — raise if a literal placeholder leaked into the prompt.

    See ``_BROKEN_PROMPT_MARKERS`` for the list. Raised as ``RuntimeError``
    so the orchestrator's normal error handling captures + logs the failure
    instead of silently feeding an inert prompt to the LLM.
    """
    for marker in _BROKEN_PROMPT_MARKERS:
        if marker in full_prompt:
            raise RuntimeError(
                f"{where} produced a prompt containing broken plumbing marker "
                f"{marker!r}; check that PR diff and test output fetches "
                f"actually executed."
            )


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
        from factory.app_config import resolve_app_repo_path
        from factory.context.loader import compose_context_prelude
        from factory.directions.parser import get_direction_chain
        from factory.runner import text_run

        persona = "reviewer"
        persona_prompt = _read_persona_prompt(persona)
        direction = find_direction_for_story(story, software_factory_root)
        chain = (
            get_direction_chain(direction, software_factory_root)
            if direction is not None
            else None
        )
        prelude = compose_context_prelude(
            persona=persona,
            app_repo_path=resolve_app_repo_path(app_config, software_factory_root),
            task_scope=story.scope,
            direction_chain=chain,
            software_factory_root=software_factory_root,
        )
        story_content = _read_story_file_content(story, software_factory_root)
        fresh_test_output = _fetch_latest_test_output(story, software_factory_root)
        pr_diff = _fetch_pr_diff_for_review(
            story, app_config, software_factory_root
        )
        full_prompt = (
            f"{persona_prompt.rstrip()}\n\n"
            "---\n\n"
            "## Context\n\n"
            f"{prelude.rstrip()}\n\n"
            "## Story\n\n"
            f"{story_content}\n\n"
            "## Test plan\n\n"
            f"{story.test_plan_json or '{}'}\n\n"
            "## Latest test output\n\n"
            f"{fresh_test_output}\n\n"
            "## PR diff\n\n"
            f"{pr_diff}\n\n"
            "Return the JSON object for the review. No prose outside the JSON."
        )
        _assert_no_broken_prompt_markers(full_prompt, where="handle_review")
        model_id = route(persona)
        result_any = text_run(
            persona=persona,
            prompt=full_prompt,
            model_id=model_id,
            schema=None,  # reviewer output is JSON but we don't enforce schema here
            max_tokens=max_output_tokens_for(model_id),
            story_id=story.id,
            app=story.app,
            direction_id=story.direction_id,
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
        # Hard convergence guard: count this request-changes verdict. Once a
        # story has accrued _MAX_REVIEW_CYCLES of them without converging, the
        # dev<->reviewer ping-pong is non-converging — stop looping back to dev
        # (which burns budget unbounded) and route to a terminal blocked state
        # for human review instead.
        story.reviewer_cycles += 1
        if story.reviewer_cycles >= _MAX_REVIEW_CYCLES:
            story.state = advance(story, EVENT_REVIEW_NONCONVERGENT).value
            story.error = (
                f"Review did not converge after {story.reviewer_cycles} reviewer "
                f"cycles (max {_MAX_REVIEW_CYCLES}); routed to "
                f"{StoryState.BLOCKED_REVIEW_NONCONVERGENT.value} for human review."
            )
            if (
                not dry_run
                and github_client is not None
                and story.github_pr_number is not None
            ):
                try:
                    repo = github_client.get_repo(app_config.repo)
                    pr = repo.get_pull(story.github_pr_number)
                    pr.add_to_labels("review-nonconvergent")
                    pr.create_issue_comment(
                        f"⚠️ Convergence guard: this PR has been through "
                        f"{story.reviewer_cycles} reviewer cycles without "
                        f"approval (max {_MAX_REVIEW_CYCLES}). The dev↔reviewer "
                        f"loop is not converging; routing to human review "
                        f"instead of dispatching dev again."
                    )
                except Exception:  # pragma: no cover - real-run path
                    pass
        elif score < 0.7:
            # Test-quality rejection: the tests themselves are wrong,
            # insufficient, or misplaced. Route to the TEST loop so
            # test_implementer rewrites them — NOT to dev, which is forbidden
            # from editing test files and would only block trying to satisfy
            # test-focused findings (the failure mode that re-blocked story 5).
            story.state = advance(story, EVENT_REVIEWER_TEST_QUALITY).value
            if (
                not dry_run
                and github_client is not None
                and story.github_pr_number is not None
            ):
                try:
                    repo = github_client.get_repo(app_config.repo)
                    pr = repo.get_pull(story.github_pr_number)
                    pr.add_to_labels("needs-test-quality-fix")
                except Exception:  # pragma: no cover - real-run path
                    pass
        else:
            story.state = advance(story, EVENT_REVIEWER_REQUEST_CHANGES).value
            if (
                not dry_run
                and github_client is not None
                and story.github_pr_number is not None
            ):
                try:
                    repo = github_client.get_repo(app_config.repo)
                    pr = repo.get_pull(story.github_pr_number)
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
        from factory.app_config import resolve_app_repo_path
        from factory.context.loader import compose_context_prelude
        from factory.directions.parser import get_direction_chain
        from factory.runner import text_run

        persona = "tech_writer"
        persona_prompt = _read_persona_prompt(persona)
        direction = find_direction_for_story(story, software_factory_root)
        chain = (
            get_direction_chain(direction, software_factory_root)
            if direction is not None
            else None
        )
        prelude = compose_context_prelude(
            persona=persona,
            app_repo_path=resolve_app_repo_path(app_config, software_factory_root),
            task_scope=story.scope,
            direction_chain=chain,
            software_factory_root=software_factory_root,
        )
        story_content = _read_story_file_content(story, software_factory_root)
        pr_diff = _fetch_pr_diff_for_review(
            story, app_config, software_factory_root
        )
        full_prompt = (
            f"{persona_prompt.rstrip()}\n\n"
            "---\n\n"
            "## Context\n\n"
            f"{prelude.rstrip()}\n\n"
            "## Story\n\n"
            f"{story_content}\n\n"
            "## PR diff\n\n"
            f"{pr_diff}\n\n"
            "Return the JSON object. No prose outside the JSON."
        )
        _assert_no_broken_prompt_markers(full_prompt, where="handle_tech_writer")
        model_id = route(persona)
        result_any = text_run(
            persona=persona,
            prompt=full_prompt,
            model_id=model_id,
            schema=None,
            max_tokens=_CHEAP_MAX_TOKENS,
            story_id=story.id,
            app=story.app,
            direction_id=story.direction_id,
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
        # Context lives in the per-story worktree so concurrent stories
        # don't collide. Each worktree shares ``.git`` with the source
        # repo so refs and history stay consistent.
        repo_path = _writing_worktree(app_config, software_factory_root, story)
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
# docs_sm — lightweight story-prep for the docs chain
# --------------------------------------------------------------------------- #


def _dry_run_docs_sm(story: StoryRecord) -> dict[str, Any]:
    """Deterministic docs-SM result for dry-run mode.

    Returns the minimum the downstream Onboarder handler needs: a story-file
    body sketch and the list of canonical paths the Onboarder should produce.
    The full BMAD story envelope isn't required — the docs path skips the
    Test-Designer entirely.
    """
    return {
        "story_file_body": (
            f"# {story.title}\n\n"
            f"## Goal\n\nProduce canonical documentation in the app repo.\n\n"
            f"## Canonical paths\n\n"
            f"- `context/project.md`\n"
            f"- `context/current-state.md`\n"
            f"- `context/navigation.md`\n"
            f"- `context/glossary.md`\n"
            f"- `context/architecture-diagrams.md`\n"
            f"- `context/sprint-status.yaml`\n"
            f"- `context/modules/<name>.md` per discovered module\n"
        ),
        "canonical_paths": [
            "context/project.md",
            "context/current-state.md",
            "context/navigation.md",
            "context/glossary.md",
            "context/architecture-diagrams.md",
            "context/sprint-status.yaml",
        ],
    }


def handle_docs_sm(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
) -> HandlerResult:
    """Lightweight SM specialized for the docs chain.

    Produces a story file under ``apps/<app>/stories/<issue>-<slug>.md`` that
    enumerates which canonical paths the Onboarder should produce. NOT a full
    BMAD story — the docs path skips test_design/test_impl/dev entirely.

    In ``dry_run``, emits a deterministic fixture. In real-run, calls
    ``text_run("sm", ...)`` with a docs-flavored prompt. The story file write
    happens BEFORE advancing state so a crash mid-write leaves the chain
    recoverable.
    """
    db = db_path or (software_factory_root / "state" / "factory.db")
    story.state = advance(story, EVENT_DOCS_SM_STARTED).value
    persist_story(story, db)

    if dry_run:
        result = _dry_run_docs_sm(story)
    else:
        # Real-run: text_run on a docs-flavored prompt. We re-use the SM
        # persona file but prefix the prompt with a directive to emit a
        # minimal docs story (skip test plans, dev notes, etc.). The SM prompt
        # already knows about canonical paths from CANONICAL_CONTEXT_PATHS.
        from factory.runner import text_run

        persona_prompt = _read_persona_prompt("sm")
        direction = find_direction_for_story(story, software_factory_root)
        direction_title = direction.title if direction else story.title
        direction_why = (direction.why if direction else "") or ""

        docs_directive = (
            "You are running in DOCS-CHAIN mode for this story. The story's "
            "deliverable is canonical documentation under the app's ``context/`` "
            "tree — NO executable code, NO test plans, NO dev notes. Produce a "
            "minimal story body listing the canonical paths the Onboarder must "
            "write and the directive's acceptance criteria verbatim. Output "
            "JSON ONLY with this shape:\n"
            '  {"story_file_body": "<markdown body>", '
            '"canonical_paths": ["context/...", "context/..."]}'
        )
        prompt = (
            f"{docs_directive}\n\n---\n\n"
            f"{persona_prompt.rstrip()}\n\n---\n\n"
            f"## Direction\n\n# {direction_title}\n\n{direction_why}\n"
        )
        schema = {
            "type": "object",
            "properties": {
                "story_file_body": {"type": "string"},
                "canonical_paths": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["story_file_body", "canonical_paths"],
        }
        result_any = text_run(
            persona="sm",
            prompt=prompt,
            model_id=route("sm"),
            schema=schema,
            max_tokens=2048,
            db_path=db,
            story_id=story.id,
            app=story.app,
            direction_id=story.direction_id,
        )
        result = result_any if isinstance(result_any, dict) else _dry_run_docs_sm(story)

    # Persist the SM JSON before any filesystem write so a crash leaves the
    # DB consistent with what the next handler will see.
    story.sm_result_json = json.dumps(result)

    # Write the story file under apps/<app>/stories/<issue>-<slug>.md.
    story_path_abs = software_factory_root / "apps" / story.app / story.story_file_path
    story_path_abs.parent.mkdir(parents=True, exist_ok=True)
    story_path_abs.write_text(result.get("story_file_body", "") + "\n", encoding="utf-8")

    story.state = advance(story, EVENT_DOCS_SM_DONE).value
    persist_story(story, db)
    return HandlerResult(next_state=StoryState(story.state), payload=result)


# --------------------------------------------------------------------------- #
# docs_onboarder — single-shot sandbox run that writes the canonical files
# --------------------------------------------------------------------------- #


def _dry_run_docs_onboarder(story: StoryRecord) -> dict[str, Any]:
    """Dry-run fixture: pretend the Onboarder wrote a plausible canonical set."""
    return {
        "files_changed": [
            "context/project.md",
            "context/current-state.md",
            "context/navigation.md",
            "context/glossary.md",
            "context/architecture-diagrams.md",
            "context/sprint-status.yaml",
        ],
        "summary": "Dry-run docs_onboarder: canonical files would be produced.",
    }


def handle_docs_onboarder(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
) -> HandlerResult:
    """Run the Onboarder persona via sandbox_run.

    The Onboarder reads the app repo, infers the project + module map, and
    writes the canonical context set on the feature branch. The single
    sandbox pass replaces the entire test_impl/dev loop the TDD chain would
    have driven.

    Uses ``resolve_app_repo_path`` for the sandbox working directory (Bug A
    fix) and ``ensure_feature_branch`` for the per-story branch (Fix 1).
    Commits produced by the sandbox land on the feature branch in the real
    app repo.
    """
    db = db_path or (software_factory_root / "state" / "factory.db")
    story.state = advance(story, EVENT_DOCS_ONBOARDER_STARTED).value
    persist_story(story, db)

    if dry_run:
        payload = _dry_run_docs_onboarder(story)
        story.state = advance(story, EVENT_DOCS_ONBOARDER_DONE).value
        story.tech_writer_result_json = json.dumps(
            {
                "context_updates": [
                    {"path": p, "action": "rewrite", "content": ""}
                    for p in payload["files_changed"]
                ]
            }
        )
        persist_story(story, db)
        return HandlerResult(next_state=StoryState(story.state), payload=payload)

    from factory.chain.branch import feature_branch_name
    from factory.runner import LLMConfig, sandbox_run

    target_repo = _writing_worktree(app_config, software_factory_root, story)
    branch = feature_branch_name(story.github_issue_number, story.slug)
    story.github_branch = branch
    persist_story(story, db)

    story_file_path_obj = software_factory_root / "apps" / story.app / story.story_file_path
    llm = LLMConfig(model=route("onboarder"))
    import asyncio

    run_res = asyncio.run(
        sandbox_run(
            persona="onboarder",
            story_path=story_file_path_obj,
            repo_path=target_repo,
            llm_config=llm,
            dry_run=False,
            story_id=story.id,
            app=story.app,
            direction_id=story.direction_id,
        )
    )

    # Onboarder writes files via SDK tool calls. The factory then explicitly
    # commits whatever's left untracked on the feature branch — the SDK
    # doesn't always commit cleanly across the full set.
    from factory.chain.branch import _run_git

    try:
        _run_git(target_repo, "add", "-A")
        # Only commit if there's anything to commit; an empty commit would be
        # a chain bug (Onboarder produced nothing) and we want to surface it.
        status = _run_git(target_repo, "status", "--porcelain").stdout.strip()
        if status:
            _run_git(
                target_repo,
                "commit",
                "-m",
                f"docs(context): bootstrap canonical context for {story.app}\n\n"
                f"Produced by Onboarder for story {story.id} ({story.slug}).",
            )
        else:
            # Nothing produced — Onboarder failed silently. Surface it.
            story.state = advance(story, EVENT_DOCS_ONBOARDER_FAILED).value
            story.error = "onboarder produced no files"
            persist_story(story, db)
            return HandlerResult(
                next_state=StoryState(story.state),
                payload={"files_changed": [], "summary": run_res.summary[-1000:]},
                error=story.error,
            )
    except Exception as exc:
        story.state = advance(story, EVENT_DOCS_ONBOARDER_FAILED).value
        story.error = f"docs_onboarder commit failed: {exc}"
        persist_story(story, db)
        return HandlerResult(
            next_state=StoryState(story.state),
            payload={"files_changed": run_res.files_changed, "summary": str(exc)},
            error=story.error,
        )

    # Capture the diff between the feature branch and base for the enforcer.
    diff_proc = _run_git(
        target_repo,
        "diff",
        "--name-only",
        f"{app_config.default_branch or 'main'}..HEAD",
    )
    diff_files = [line.strip() for line in diff_proc.stdout.splitlines() if line.strip()]

    # Persist the file list under tech_writer_result_json so the existing
    # ``handle_docs_enforcer`` can pick it up unchanged — the field name is
    # legacy; semantically it's "list of files this story touched".
    story.tech_writer_result_json = json.dumps(
        {"context_updates": [{"path": p, "action": "rewrite", "content": ""} for p in diff_files]}
    )

    story.state = advance(story, EVENT_DOCS_ONBOARDER_DONE).value
    persist_story(story, db)
    payload = {
        "files_changed": diff_files,
        "summary": run_res.summary[-2000:],
        "tokens_in": run_res.tokens_in,
        "tokens_out": run_res.tokens_out,
        "cost_usd": run_res.cost_usd,
    }
    return HandlerResult(next_state=StoryState(story.state), payload=payload)


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
    Clean → PR_OPEN. Opens the GitHub PR programmatically when reached via
    the docs chain (TDD path leaves PR creation to a future webhook).

    In dry-run mode without an explicit ``pr_files`` list, we derive a
    plausible set from the tech_writer result (so the enforcer sees the
    same paths the tech_writer claimed to write). Docs chain populates the
    same JSON field with its diff file list.
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

    # Docs chain branches here: when the enforcer reaches this point coming
    # from the docs chain, open the PR programmatically. The TDD path
    # historically leaves PR creation to a separate worker; we mirror that
    # behavior here when the chain_kind is "tdd".
    if (
        not dry_run
        and story.chain_kind == "docs"
        and story.github_pr_number is None
        and story.github_branch
    ):
        opened = _open_pr_for_docs_story(story, app_config, software_factory_root)
        if opened is not None:
            story.github_pr_number = opened
            persist_story(story, db)
            payload["pr_number"] = opened

    story.state = advance(story, EVENT_DOCS_ENFORCER_PASS).value
    persist_story(story, db)
    return HandlerResult(next_state=StoryState(story.state), payload=payload)


def _post_factory_needs_redesign_comment(
    *,
    story: StoryRecord,
    app_config: AppConfig,
    suggestions: list[str],
    last_tail: str,
    software_factory_root: Path,
) -> None:
    """Post a 'factory needs redesign' comment on the direction's tracker issue.

    Operator-loud signal that the chain ran out of dev retries on a small
    story — likely the factory itself needs work (test_implementer wrote
    impossible tests, persona prompt missing context, env/runtime gap,
    etc.) rather than dev needing more chances.

    Best-effort: silently no-ops when ``gh`` isn't on PATH, the direction
    has no tracker_issue recorded, or the call fails. The structured
    event log entry is the durable record; this comment is the surface.
    """
    direction = find_direction_for_story(story, software_factory_root)
    if direction is None:
        return
    tracker = direction.state.get("tracker_issue") if hasattr(direction, "state") else None
    if not tracker:
        return
    repo = f"{app_config.repo_owner}/{app_config.repo_name}"
    body_parts = [
        f"## :warning: factory_needs_redesign — story #{story.id} ({story.slug})",
        "",
        f"Dev exhausted **{story.dev_retries}/{_MAX_DEV_RETRIES}** chain-level "
        f"retries with prior-attempt feed-forward enabled. That's a strong "
        f"signal something upstream of dev needs work — not that dev needs more chances.",
        "",
        f"- Branch: `{story.github_branch}`",
        f"- Story slug: `{story.slug}`",
        f"- Direction: `{story.direction_id}`",
        "",
        "### Suggestions",
    ]
    for s in suggestions:
        body_parts.append(f"- {s}")
    if last_tail.strip():
        body_parts += [
            "",
            "### Last test-output tail",
            "```",
            last_tail[-1500:],
            "```",
        ]
    body_parts.append("")
    body_parts.append(
        f"Inspect via `factory trace {story.id}` for the per-attempt event log."
    )
    body = "\n".join(body_parts)

    import subprocess

    subprocess.run(
        ["gh", "issue", "comment", str(tracker), "--repo", repo, "--body", body],
        check=False,
        timeout=30,
        capture_output=True,
    )


def _open_pr_for_docs_story(
    story: StoryRecord, app_config: AppConfig, software_factory_root: Path
) -> int | None:
    """Push the feature branch and open a PR via the ``gh`` CLI.

    Returns the PR number on success, ``None`` on any failure. Failures are
    swallowed — the chain still transitions to ``PR_OPEN`` and the operator
    can open the PR by hand from the (already-pushed) branch.

    Uses ``gh`` rather than pygithub because the chain runs locally with
    ``gh auth login`` already configured; no extra token plumbing needed.

    Pushes from the per-story worktree (same one the onboarder ran in)
    rather than the source repo — that's where the branch's HEAD is.
    """
    import subprocess

    target_repo = _writing_worktree(app_config, software_factory_root, story)
    base = app_config.default_branch or "main"
    branch = story.github_branch
    title = f"docs(context): {story.title}"
    body = (
        f"Generated by factory docs chain for story #{story.github_issue_number}.\n\n"
        f"Branch: `{branch}`\n"
        f"Story slug: `{story.slug}`\n"
        f"Direction: `{story.direction_id}`\n\n"
        f"Canonical context files added/updated. Auto-merge is OFF — review and merge by hand."
    )
    try:
        # Push the branch first; gh pr create needs an upstream ref.
        subprocess.run(
            ["git", "push", "-u", "origin", str(branch)],
            cwd=str(target_repo),
            check=True,
            capture_output=True,
            timeout=60,
        )
        # Emit push signal — best-effort.
        try:
            from factory.manager.signals import write_git_event as _wge

            _wge(
                kind="push",
                story_id=story.id,
                worktree_path=str(target_repo),
                result="ok",
                software_factory_root=software_factory_root,
            )
        except Exception:  # noqa: BLE001
            pass

        proc = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                app_config.repo,
                "--base",
                base,
                "--head",
                str(branch),
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=str(target_repo),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as _push_exc:
        # Emit error signal — best-effort.
        try:
            from factory.manager.signals import write_git_event as _wge_err

            _wge_err(
                kind="pr_open",
                story_id=story.id,
                worktree_path=str(target_repo),
                result="error",
                error=repr(_push_exc),
                software_factory_root=software_factory_root,
            )
        except Exception:  # noqa: BLE001
            pass
        return None

    # gh prints the PR URL on stdout; parse the trailing digits.
    url = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    # URL shape: https://github.com/<org>/<repo>/pull/<n>
    m = re.search(r"/pull/(\d+)$", url)
    pr_num = int(m.group(1)) if m else None

    # Emit pr_open signal — best-effort.
    try:
        from factory.manager.signals import write_git_event as _wge_pr

        _wge_pr(
            kind="pr_open",
            story_id=story.id,
            pr_number=pr_num,
            result="ok",
            software_factory_root=software_factory_root,
        )
    except Exception:  # noqa: BLE001
        pass

    return pr_num


# --------------------------------------------------------------------------- #
# helpers used by the orchestrator
# --------------------------------------------------------------------------- #


def find_direction_for_story(story: StoryRecord, software_factory_root: Path) -> Direction | None:
    """Look up the parsed Direction record for a story (by direction_id)."""
    for dpath in list_direction_dirs(story.app, software_factory_root):
        if dpath.name.startswith(f"{story.direction_id}-"):
            return parse_direction_dir(
                story.app, dpath, software_factory_root=software_factory_root
            )
    return None


# --------------------------------------------------------------------------- #
# Phase 5 — handle_deploy
# --------------------------------------------------------------------------- #


def _lookup_merged_sha(app: str, pr_number: int, db_path: Path) -> str | None:
    """Return the head_sha of the most-recent merged ``merge_actions`` row.

    Returns ``None`` when no merge has been recorded for this PR yet.
    Imported lazily to avoid a chain → auto_merge import cycle.
    """
    from factory.chain.auto_merge import MergeActionRecord

    eng = create_engine(f"sqlite:///{db_path}", echo=False)
    SQLModel.metadata.create_all(eng)
    with Session(eng) as session:
        row = session.exec(
            select(MergeActionRecord)
            .where(
                MergeActionRecord.app == app,
                MergeActionRecord.pr_number == pr_number,
                MergeActionRecord.merged == True,  # noqa: E712
            )
            .order_by(MergeActionRecord.id.desc())  # type: ignore[union-attr]
        ).first()
    if row is None:
        return None
    return row.head_sha


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

    # Look up the merged_sha from the ``merge_actions`` row this PR
    # produced. We pick the most-recent ``merged=True`` row for this PR.
    # If no row exists, refuse to dispatch — deploying without knowing
    # the SHA we just merged is a category error (we'd be deploying an
    # unspecified commit). The webhook path inserts a ``merge_actions``
    # row at PR-merged time, and tests can seed one via
    # ``factory.chain.auto_merge.MergeActionRecord`` directly.
    #
    # IMPORTANT: do this BEFORE advancing to deploy_in_progress so the
    # story stays in DEPLOY_PENDING (recoverable) when the SHA isn't
    # there yet. Otherwise we'd advance to deploy_in_progress, leaving
    # the chain with no way to resume that PR's deploy.
    merged_sha = _lookup_merged_sha(story.app, story.github_pr_number, db)
    if merged_sha is None:
        # Allow dry-run a fallback so unit tests that don't seed a
        # merge_actions row can still exercise the success/failure paths.
        if dry_run:
            merged_sha = "pending-sha"
        else:
            err = f"merge SHA not recorded for PR {story.github_pr_number}"
            story.error = err
            persist_story(story, db)
            return HandlerResult(
                next_state=StoryState(story.state),
                error=err,
            )

    # Mark started.
    story.state = advance(story, EVENT_DEPLOY_STARTED).value
    persist_story(story, db)

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
