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
import logging
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
    EVENT_REVIEW_NONCONVERGENT,
    EVENT_REVIEWER_APPROVE,
    EVENT_REVIEWER_REQUEST_CHANGES,
    EVENT_REVIEWER_STARTED,
    EVENT_SM_DONE,
    EVENT_SM_STARTED,
    EVENT_TECH_WRITER_DONE,
    EVENT_TECH_WRITER_STARTED,
    StoryRecord,
    StoryState,
    advance,
)
from factory.chain.worktree import ensure_worktree_for_story
from factory.context.enforcer import format_violation_comment, scan_pr_diff
from factory.context.updater import ContextUpdate, apply_context_updates
from factory.directions.parser import Direction, list_direction_dirs, parse_direction_dir
from factory.model_router import max_output_tokens_for, route

_logger = logging.getLogger(__name__)

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
    import subprocess

    from factory.app_config import resolve_app_repo_path

    source_repo = resolve_app_repo_path(app_config, software_factory_root)
    wt = ensure_worktree_for_story(
        source_repo,
        software_factory_root=software_factory_root,
        app=story.app,
        story_id=story.github_issue_number,
        slug=story.slug,
        base_branch=app_config.default_branch or "main",
    )

    # Refresh the worktree against the CURRENT base before the writing handler
    # runs. ensure_worktree_for_story's reuse path returns a worktree cut from
    # an OLD base; when a fix merges to main while a story is mid-flight (e.g. a
    # sibling un-breaks the test suite), that in-flight story's dev run keeps
    # testing against the stale base and can never go green — the dev churns on
    # a failure it cannot fix. The PR-open path already re-merges base at PR
    # time (see _open_pr_for_story); do the same here so every dev/writing
    # attempt sees since-merged fixes. Best-effort and NON-destructive: `git
    # merge` preserves uncommitted WIP that doesn't overlap the merged files,
    # and on ANY conflict/dirty-overlap we abort and proceed on the current
    # base (never silently clobber the dev's in-progress work).
    base = app_config.default_branch or "main"
    try:
        subprocess.run(
            ["git", "fetch", "origin", base],
            cwd=str(wt), check=False, capture_output=True, timeout=60,
        )
        merged = subprocess.run(
            ["git", "merge", "--no-edit", f"origin/{base}"],
            cwd=str(wt), capture_output=True, text=True, timeout=120,
        )
        if merged.returncode != 0:
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=str(wt), check=False, capture_output=True, timeout=60,
            )
    except (OSError, subprocess.SubprocessError):
        pass  # base refresh is best-effort; never block the writing handler
    finally:
        # CRITICAL: if the merge timed out or the process was killed mid-merge,
        # the except above skips the return-code abort and leaves the worktree
        # with MERGE_HEAD + an unmerged index. A later `git add -A` + commit in
        # a writing handler would then bake unresolved conflict markers into a
        # real commit and push them (silent corruption, worse than a stale
        # base). Guarantee no in-progress merge is ever left behind, however
        # the merge call exited.
        _abort_inflight_merge(wt)
    return wt


def _abort_inflight_merge(worktree: Path) -> None:
    """Best-effort: clear a left-in-progress merge (MERGE_HEAD present) so no
    downstream ``git add -A`` + commit can bake conflict markers into a real
    commit. Idempotent — a no-op when no merge is in progress."""
    import subprocess

    try:
        head = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            cwd=str(worktree), capture_output=True, timeout=15,
        )
        if head.returncode == 0:
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=str(worktree), check=False, capture_output=True, timeout=30,
            )
    except (OSError, subprocess.SubprocessError):
        pass


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
    # Review-cycle history (last 4 cycles) — reviewer finality memory + dev's
    # "already addressed" digest. See StoryRecord.reviewer_history_json.
    "reviewer_history_json": "TEXT",
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
            "## YOUR ASSIGNMENT — exactly ONE story file\n\n"
            "This invocation prepares the story file for exactly ONE StoryRecord\n"
            "(the chain runs you once per record). The PM result's child_stories\n"
            "above are decomposition CONTEXT — scope boundaries and sequencing —\n"
            "NOT a list of files to emit. Your `stories` array MUST contain\n"
            "EXACTLY ONE entry, with `slug` set EXACTLY to the value below\n"
            "(verbatim — the chain matches on it and refuses to write on\n"
            "mismatch). If this record is one interpretation of a dual-draft\n"
            "pair (title suffixed 'narrow read'/'broad read'), scope the story\n"
            "content to THAT interpretation.\n\n"
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
            db_path=db,
        )
        if not isinstance(result_any, dict):
            return HandlerResult(
                next_state=StoryState(story.state),
                error="sm returned non-dict",
            )
        result = result_any

    # Find the story entry that matches this StoryRecord. The DB slug is
    # TRUNCATED (column cap) while the SM emits full slugs, so exact equality
    # misses (e.g. "...llm-match-cont" vs "...llm-match-contract") — match by
    # prefix in either direction. And NEVER fall back to "the first story":
    # that silent default wrote the FIRST sibling's contract into 9 stories'
    # files (D009/D010, found 2026-06-11) — dev faithfully re-built story
    # 22's scope under story 23's title, the reviewer approved it against the
    # same wrong file, and the duplicate PR conflicted with the real merged
    # implementation. Writing the wrong contract is strictly worse than
    # failing loudly.
    stories_out = result.get("stories") or []
    matched: dict[str, Any] | None = None
    for s in stories_out:
        s_slug = str(s.get("slug") or "")
        if s_slug == story.slug or (
            s_slug and (s_slug.startswith(story.slug) or story.slug.startswith(s_slug))
        ):
            matched = s
            break

    if matched is None:
        # Slug mismatch: do NOT advance. Advancing to SM_DONE without a
        # written file poisons dispatch forever (dev dies on
        # FileNotFoundError every tick — observed 2026-07-17 when dual-draft
        # records met an SM that emitted per-child_story files). Reset to
        # STORY_CREATED so the next tick re-runs SM against the (now
        # single-story-explicit) prompt; the mismatch is loud in the story
        # event log either way.
        story.error = (
            f"sm output has no story matching slug {story.slug!r} "
            f"(got: {[str(s.get('slug'))[:40] for s in stories_out[:6]]!r}); "
            "refusing to write a sibling's contract into this story's file"
        )
        story.state = StoryState.STORY_CREATED.value
        story.sm_result_json = json.dumps(result)
        log_story_event(
            story.id,
            "sm_slug_mismatch",
            {
                "expected_slug": story.slug,
                "got_slugs": [str(s.get("slug"))[:60] for s in stories_out[:12]],
            },
            software_factory_root=software_factory_root,
            slug_hint=story.slug,
        )
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



def _finding_location_file(f: dict[str, Any]) -> str:
    """Normalize a finding's location to its file path (drop :line, lowercase).

    Line numbers shift as dev edits; the FILE is the stable identity of where
    a defect lives. Findings without a location normalize to "" (excluded
    from location-set comparisons).
    """
    loc = str(f.get("location") or f.get("file") or "").strip().lower()
    return loc.split(":", 1)[0].strip()


def _findings_signature(result: dict[str, Any]) -> str:
    """Stable hash of a review's actionable findings (order-independent).

    Keys on normalized location FILE + ``what``/``issue`` text: two cycles
    flagging the same site count as "same" even when the reviewer rewords the
    complaint (rewording defeated the text-only signature — benchmark t3,
    2026-07-17, six cycles with consecutive_same=1 throughout).
    """
    import hashlib

    parts = []
    for f in (result.get("findings") or []) + (result.get("test_quality_findings") or []):
        if isinstance(f, dict):
            loc = _finding_location_file(f)
            what = (f.get("what") or f.get("issue") or "").strip().lower()[:160]
            parts.append(f"{loc}|{what}" if loc else what)
    digest = hashlib.sha256(" ".join(sorted(parts)).encode("utf-8", "replace"))
    return digest.hexdigest()[:16]


_REVIEWER_HISTORY_MAX_CYCLES = 4


def _append_reviewer_history(story: StoryRecord, result: dict[str, Any]) -> None:
    """Append this cycle's review to ``story.reviewer_history_json`` (capped).

    Stores a digest, not the full result: enough for the reviewer to honor
    finality (location + what + severity + regression flag) and for dev's
    do-not-regress section, without unbounded prompt growth.
    """
    def _digest(findings: Any, *, is_test: bool = False) -> list[dict[str, Any]]:
        out = []
        for f in findings or []:
            if not isinstance(f, dict):
                continue
            out.append(
                {
                    "severity": f.get("severity", "medium" if not is_test else "low"),
                    "location": str(f.get("location") or f.get("test_name") or "")[:160],
                    "what": str(f.get("what") or f.get("issue") or "")[:200],
                    "regression": bool(f.get("regression")),
                }
            )
        return out

    entry = {
        "cycle": story.reviewer_cycles,
        "verdict": result.get("verdict", "request_changes"),
        "score": result.get("test_quality_score"),
        "findings": _digest(result.get("findings")),
        "test_quality_findings": _digest(result.get("test_quality_findings"), is_test=True),
    }
    try:
        history = json.loads(story.reviewer_history_json or "[]")
        if not isinstance(history, list):
            history = []
    except (json.JSONDecodeError, TypeError):
        history = []
    history.append(entry)
    story.reviewer_history_json = json.dumps(history[-_REVIEWER_HISTORY_MAX_CYCLES:])


def _render_reviewer_history_section(story: StoryRecord) -> str:
    """The "Your previous findings" prompt section for re-reviews.

    Empty string on the first review. This is what makes the persona's
    finality rule ENFORCEABLE — without it the reviewer has no memory of its
    own prior verdicts and re-derives fresh objections every cycle.
    """
    try:
        history = json.loads(story.reviewer_history_json or "[]")
    except (json.JSONDecodeError, TypeError):
        history = []
    if not history:
        return ""
    lines = [
        "## Your previous findings (this is a RE-REVIEW — read before judging)",
        "",
        "You have reviewed this story before. Per the Review-finality rule, a",
        "new `medium`/`high` finding is legitimate ONLY as (a) a regression",
        "since your last review (mark `\"regression\": true`) or (b) one of",
        "the findings below not actually addressed. Anything else is `low` /",
        "`comments_to_post`. At cycle 3+ the chain clamps non-compliant",
        "blocking findings to non-blocking.",
        "",
    ]
    for entry in history:
        lines.append(
            f"### Cycle {entry.get('cycle')} — verdict: {entry.get('verdict')}"
        )
        for f in (entry.get("findings") or []) + (entry.get("test_quality_findings") or []):
            reg = " (regression)" if f.get("regression") else ""
            lines.append(
                f"- [{f.get('severity')}] {f.get('location')}: {f.get('what')}{reg}"
            )
        lines.append("")
    return "\n".join(lines)


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

# Dev retry budget. A red dev run (tests not green) consumes one retry; at the
# cap the story blocks with an explicit ``factory_needs_redesign`` event so the
# operator sees the signal. 6 (operator-approved 2026-05-29): many stories land
# 1-N tests short and the extra informed attempts (dev receives prior-attempt
# history + reviewer findings) materially improve convergence on harder stories.
_MAX_DEV_RETRIES = 6

# Review convergence guard. Judged by finding STABILITY, not raw cycle count: a
# cycle that surfaces DIFFERENT findings is making progress. We block only when
# the reviewer returns the SAME findings _MAX_REVIEW_STUCK times in a row
# (genuine churn — "nothing loops unproductively >3"), with a hard absolute
# backstop (_MAX_REVIEW_CYCLES) so a slowly-mutating loop can't run forever.
_MAX_REVIEW_STUCK = 3
_MAX_REVIEW_CYCLES = 6



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

    With ``dev_convergence.enabled`` (factory_settings.yaml), a red run
    retries IMMEDIATELY in this same invocation — fresh sandbox, prior-
    attempts memory carried forward by ``_handle_dev_once``'s normal red
    bookkeeping — instead of waiting for the next tick. The loop grants no
    extra attempts (``_MAX_DEV_RETRIES`` stays authoritative); it only
    removes the tick-cadence dead time between the same retries. Guards:
    inner-attempt cap, one retry of headroom under the chain cap, per-story
    wall-clock + budget, and a live re-check of the global hourly/daily
    spend caps. Infra failures and content-filter escalations always exit
    the loop into their existing across-ticks paths.

    ``force_red`` is for testing the retry path: when True, the dry-run
    branch returns tests_green=False so the handler exercises the retry +
    escalation logic.
    """
    import time as _time

    from factory.settings.loader import load_settings

    result = _handle_dev_once(
        story,
        app_config,
        software_factory_root,
        dry_run=dry_run,
        db_path=db_path,
        force_red=force_red,
    )
    conv = load_settings(software_factory_root).dev_convergence
    if dry_run or not conv.enabled:
        return result

    db = db_path or (software_factory_root / "state" / "factory.db")
    loop_started_mono = _time.monotonic()
    loop_started_ts = datetime.now(UTC).isoformat()
    inner_attempts = 1

    while True:
        stop_reason = _dev_inner_loop_stop_reason(
            story=story,
            result=result,
            conv=conv,
            inner_attempts=inner_attempts,
            elapsed_s=_time.monotonic() - loop_started_mono,
            since_ts=loop_started_ts,
            software_factory_root=software_factory_root,
            db_path=db,
        )
        if stop_reason == "continue":
            inner_attempts += 1
            log_story_event(
                story.id,
                "dev_inner_attempt",
                {"inner_attempt": inner_attempts, "dev_retries": story.dev_retries},
                software_factory_root=software_factory_root,
                slug_hint=story.slug,
            )
            result = _handle_dev_once(
                story,
                app_config,
                software_factory_root,
                dry_run=dry_run,
                db_path=db_path,
                force_red=force_red,
            )
            continue
        if stop_reason != "terminal":
            # Stopped while the story is still retryable — record why so the
            # FMS can distinguish "loop budget ran out" from "story done".
            log_story_event(
                story.id,
                "dev_inner_loop_stopped",
                {
                    "reason": stop_reason,
                    "inner_attempts": inner_attempts,
                    "dev_retries": story.dev_retries,
                },
                software_factory_root=software_factory_root,
                slug_hint=story.slug,
            )
        return result


def _dev_inner_loop_stop_reason(
    *,
    story: StoryRecord,
    result: HandlerResult,
    conv: Any,
    inner_attempts: int,
    elapsed_s: float,
    since_ts: str,
    software_factory_root: Path,
    db_path: Path,
) -> str:
    """Decide whether the dev convergence loop re-attempts now.

    Returns ``"continue"`` to re-dispatch immediately, ``"terminal"`` when
    the story left the retryable state (green, exhausted, blocked), or a
    stop-reason label when the story IS retryable but a loop guard says to
    fall back to the normal across-ticks path.
    """
    if StoryState(story.state) is not StoryState.DEV_RETRY:
        return "terminal"
    payload = result.payload or {}
    if payload.get("sandbox_infra_error"):
        return "infra_failure"
    if payload.get("content_filter_tier_escalation"):
        return "content_filter"
    if inner_attempts >= conv.max_inner_attempts:
        return "attempts_cap"
    # Leave the LAST chain retry to the normal tick path so exhaustion
    # bookkeeping (WIP commit/push, factory_needs_redesign) never runs
    # inside a tight loop iteration.
    if story.dev_retries + 1 >= _MAX_DEV_RETRIES:
        return "retry_headroom"
    if elapsed_s >= conv.per_story_wall_clock_s:
        return "wall_clock"
    story_cost = _story_spend_since(db_path, story.id, since_ts)
    if story_cost >= conv.per_story_budget_usd:
        return "budget"
    # Live re-check of the global caps: the settings enforcer only gates
    # dispatch, so a tight loop must re-verify mid-flight.
    from factory.settings.loader import load_settings
    from factory.settings.spend import hour_spend_usd, today_spend_usd

    caps = load_settings(software_factory_root).caps
    if hour_spend_usd(software_factory_root, db_path=db_path) >= caps.hourly_spend_usd:
        return "hourly_cap"
    if today_spend_usd(software_factory_root, db_path=db_path) >= caps.daily_spend_usd:
        return "daily_cap"
    return "continue"


def _story_spend_since(db_path: Path, story_id: int | None, since_ts: str) -> float:
    """Sum ``runs.cost_usd`` for ``story_id`` with ``ts >= since_ts`` (best-effort)."""
    if story_id is None:
        return 0.0
    try:
        from factory.runner import Run, _engine

        total = 0.0
        with Session(_engine(db_path)) as session:
            rows = session.exec(select(Run).where(Run.story_id == story_id)).all()
            for r in rows:
                if (r.ts or "") >= since_ts:
                    total += float(r.cost_usd or 0.0)
        return total
    except Exception:
        # A broken ledger must not stop the loop-guard decision; treat as
        # budget-exhausted so the loop stops rather than spending blind.
        return float("inf")


def _handle_dev_once(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    force_red: bool = False,
) -> HandlerResult:
    """Single dev attempt: dispatch sandbox, check tests, do retry/escalation
    bookkeeping. See ``handle_dev`` for the convergence-loop wrapper.
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
        from factory.chain.branch import feature_branch_name
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
        # fix and the dev<->reviewer loop cannot converge. The most recent
        # verdict carries the actionable items; earlier cycles ride along as a
        # ``prior_cycles`` digest so already-fixed sites don't regress.
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
        if reviewer_findings is not None and story.reviewer_history_json:
            try:
                history = json.loads(story.reviewer_history_json)
                # The final history entry is the latest review — already fully
                # rendered as the actionable findings; only OLDER cycles go in
                # the do-not-regress digest.
                if isinstance(history, list) and len(history) > 1:
                    reviewer_findings["prior_cycles"] = history[:-1]
            except (json.JSONDecodeError, TypeError):
                pass

        from factory.settings.loader import load_settings as _load_settings

        _dev_timeout_s = _load_settings(software_factory_root).dev_convergence.dev_sandbox_timeout_s

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
                wall_clock_timeout_s=_dev_timeout_s,
                db_path=db,
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
            # Provider content-filter blocks are NOT infrastructure: the same
            # conversation deterministically re-trips the filter (observed
            # 2026-06-11, Azure "ResponsibleAI" blocking deepseek's own
            # completion 3/3 on story 32), so retrying the same model just
            # marches to the breaker cap and terminally blocks the story.
            # Escalate to the hard-tier model instead — a different model has
            # a different filter profile. One-shot: if the hard tier also
            # filters, fall through to the bounded infra path below.
            if (
                "content_filter" in (run_res.error or "")
                and story.current_model_tier != "hard"
            ):
                story.current_model_tier = "hard"
                story.state = advance(story, EVENT_DEV_TESTS_RED).value
                story.error = None
                persist_story(story, db)
                log_story_event(
                    story.id,
                    "dev_content_filter_tier_escalation",
                    {
                        "to_tier": "hard",
                        "error": (run_res.error or "")[:300],
                    },
                    software_factory_root=software_factory_root,
                    slug_hint=story.slug,
                )
                return HandlerResult(
                    next_state=StoryState(story.state),
                    payload={
                        "test_run_passed": False,
                        "content_filter_tier_escalation": True,
                    },
                    error=None,
                )
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

        # Loop-4 (dev-owns-tests): the dev persona writes BOTH production code
        # and its tests, so there is no "tests are frozen" invariant to enforce
        # and no Test-Designer to route a clarification back to. A failed run is
        # simply a dev retry (below). Test *quality* (no slop, meaningful
        # assertions) is gated downstream by the reviewer + programmatic slop
        # detector, not here.
        tests_green = bool(run_res.test_run_passed)
        payload = {
            "files_changed": run_res.files_changed,
            "test_run_passed": tests_green,
            "summary": run_res.summary[-2000:],
        }

        # Loop-4: a dev run that ends red — including one that made no code
        # changes — is just a failed attempt and consumes a retry below. There
        # is no longer a separate test author to route a "tests are wrong"
        # signal back to: the dev owns the tests and fixes them on the next
        # attempt. The no-change case still exhausts the budget quickly because
        # ``_MAX_DEV_RETRIES`` caps total attempts.

    if tests_green:
        # Record the GREEN run's test output too. The reviewer's
        # ``_fetch_latest_test_output`` prefers the last dev_attempts_json
        # entry; when only red attempts were recorded, a story with any red
        # history showed the reviewer stale failures forever — story 32 was
        # rejected on 2026-06-11 over a "still 12 failing tests" tail that
        # was ten days old.
        if not dry_run:
            green_record = {
                "attempt": story.dev_retries,
                "ts": datetime.now(UTC).isoformat(),
                "test_run_passed": True,
                "files_touched": (run_res.files_changed or [])[:20],
                "test_output_tail": (run_res.summary or "")[-1800:],
                "summary": "tests green",
                "self_summary": (getattr(run_res, "self_summary", "") or "")[:2000],
            }
            try:
                prior_green = json.loads(story.dev_attempts_json or "[]")
                if not isinstance(prior_green, list):
                    prior_green = []
            except (json.JSONDecodeError, TypeError):
                prior_green = []
            prior_green.append(green_record)
            story.dev_attempts_json = json.dumps(prior_green[-5:])
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
            "test_run_passed": False,
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
                passed = last.get("test_run_passed")
                verdict = (
                    "PASSED" if passed is True
                    else "FAILED" if passed is False
                    else "unknown"
                )
                header = (
                    f"(from dev_attempts[-1]; "
                    f"attempt={last.get('attempt')!r} "
                    f"ts={last.get('ts')!r} "
                    f"run_verdict={verdict})\n"
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


def _has_real_dev_attempt(story: StoryRecord) -> bool:
    """True when at least one REAL (non-dry-run) dev attempt is on record.

    ``dev_attempts_json`` is only ever appended to by ``_handle_dev_once``
    on a non-dry-run completion — both the green-run record and the
    red-run/retry record are gated behind ``if not dry_run:`` (see
    ``_handle_dev_once``). So a non-empty list here is proof a real dev
    sandbox actually ran, which is the signal the empty-diff short-circuit
    needs before it's allowed to fire — it must never trigger on a story
    that hasn't given dev a real chance yet.
    """
    try:
        parsed = json.loads(story.dev_attempts_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, list) and len(parsed) > 0


def _dev_produced_empty_diff(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
) -> bool | None:
    """Best-effort check: is the story's branch diff-empty against its base?

    Returns ``True`` when the COMMITTED diff (``git diff --quiet
    origin/<base>...HEAD``) AND the working tree are both empty — i.e. the
    dev genuinely produced nothing at all — ``False`` when either a real
    committed diff OR uncommitted/untracked working-tree changes exist, or
    ``None`` when the check itself could not be performed (no worktree yet,
    git error, unexpected exit code, missing ``origin/<base>`` ref). ``None``
    means "unknown" — callers MUST treat it as "do not short-circuit" and
    fall back to the normal review path.

    The working-tree check matters because the dev's "work happened" signal
    is commit-agnostic: ``files_changed``/``test_run_passed`` come from the
    on-disk working tree (``git status --porcelain`` / the pytest run
    against it), but the dev agent is only INSTRUCTED to commit, not forced
    to. An agent that produces real, test-passing work but never runs its
    final ``git commit`` (e.g. it hit a turn/timeout limit) would otherwise
    show an empty COMMITTED diff and get permanently blocked on its first
    pass — worse than the review churn this check exists to prevent. So an
    uncommitted/untracked change in the worktree is treated the same as a
    committed one: NOT empty, fall back to the normal review path (which at
    least gives dev more retries to land the commit).
    """
    import subprocess

    try:
        worktree = _writing_worktree(app_config, software_factory_root, story)
    except Exception:  # noqa: BLE001 - best-effort, fail open
        return None

    # Working-tree check FIRST: uncommitted/untracked changes mean dev did
    # real work even if it never committed. Never short-circuit on that.
    try:
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if status_proc.returncode != 0:
        return None
    if status_proc.stdout.strip():
        return False

    base = app_config.default_branch or "main"
    try:
        proc = subprocess.run(
            ["git", "diff", "--quiet", f"origin/{base}...HEAD"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    # ``git diff --quiet`` exits 0 (no diff), 1 (diff present), or >1 on a
    # real error (e.g. bad/missing ref) — only 0/1 are meaningful signals.
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


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


def _slop_findings_for_story(
    story: StoryRecord, app_config: AppConfig, software_factory_root: Path
) -> list[dict[str, Any]]:
    """Programmatically scan the dev-written test files for slop anti-patterns.

    Loop-4 backstop for the dev-owns-tests model: scans the test files this
    story's branch changed (relative to ``origin/main``) with the pure
    ``slop_detector``. Returns a list of ``test_quality_findings``-shaped dicts
    (empty when clean). Defensive: any git/IO failure returns ``[]`` so a
    transient infra problem never blocks a review — the LLM reviewer remains
    the primary judge; this only ADDS deterministic vetoes, never removes them.
    """
    try:
        from factory.chain.branch import find_test_files_in_diff
        from factory.chain.slop_detector import scan_file

        worktree = _writing_worktree(app_config, software_factory_root, story)
        base_ref = None
        for candidate in ("origin/main", "main", "HEAD~1"):
            try:
                from factory.chain.branch import _run_git

                _run_git(worktree, "rev-parse", "--verify", candidate)
                base_ref = candidate
                break
            except Exception:
                continue
        if base_ref is None:
            return []
        test_paths = find_test_files_in_diff(worktree, base_ref=base_ref)
        findings: list[dict[str, Any]] = []
        for rel in test_paths:
            for f in scan_file(worktree / rel):
                findings.append(
                    {
                        "test_name": f"{f.path}:{f.line}",
                        "issue": f"slop: {f.kind} — {f.why_slop}",
                        "fix_suggestion": (
                            "Replace this with an assertion on the real behavior "
                            "of the code under test. Make the test fail against an "
                            "absent/empty implementation first, then pass."
                        ),
                        "code_excerpt": f.code_excerpt,
                    }
                )
        return findings
    except Exception:  # pragma: no cover - defensive; never block on infra
        return []


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

    # Empty-diff short-circuit. Real (non-fixture, non-dry-run) reviews only:
    # fixture-driven and dry-run tests never touch git and must be
    # completely unaffected. When a REAL dev attempt has already run
    # (``_has_real_dev_attempt``) and the branch's diff against its base is
    # CONFIRMED empty (``_dev_produced_empty_diff`` returns True, never on
    # an unknown/error result), the dev produced no changes at all.
    # Continuing into the normal review flow would waste an LLM call only to
    # have the reviewer correctly reject it, then churn a full
    # request_changes cycle, repeating up to ``_MAX_REVIEW_CYCLES`` times
    # before blocking (observed: D092/D094 each burned ~6 empty-diff cycles).
    # Escalate straight to the terminal blocked state on the FIRST
    # occurrence instead.
    if fixture is None and not dry_run and _has_real_dev_attempt(story):
        empty_diff = _dev_produced_empty_diff(story, app_config, software_factory_root)
        if empty_diff is True:
            reason = (
                f"empty diff after dev attempt {story.dev_retries} — the dev "
                "produced no changes; escalating instead of churning review "
                "cycles; likely an un-doable-by-sandbox (runtime/operational) "
                "task or a malformed story."
            )
            story.state = advance(story, EVENT_REVIEW_NONCONVERGENT).value
            story.error = reason
            result = {
                "verdict": "request_changes",
                "summary": reason,
                "findings": [],
                "test_quality_findings": [],
                "test_quality_score": 0.0,
                "empty_diff_short_circuit": True,
            }
            story.reviewer_result_json = json.dumps(result)
            _append_reviewer_history(story, result)
            persist_story(story, db)
            log_story_event(
                story.id,
                "factory_needs_redesign",
                {
                    "kind": "empty_diff_short_circuit",
                    "dev_retries": story.dev_retries,
                    "reviewer_cycles": story.reviewer_cycles,
                    "branch": story.github_branch,
                    "suggestions": [
                        "Dev produced an empty diff after a real attempt — "
                        "likely a task that isn't doable by a code sandbox "
                        "(a runtime/operational task) or a malformed story. "
                        "Investigate before re-dispatching; do not just "
                        "retry the same chain.",
                    ],
                },
                software_factory_root=software_factory_root,
                slug_hint=story.slug,
            )
            if not dry_run and github_client is not None and story.github_pr_number is not None:
                try:
                    repo = github_client.get_repo(app_config.repo)
                    pr = repo.get_pull(story.github_pr_number)
                    pr.add_to_labels("review-nonconvergent")
                    pr.create_issue_comment(
                        "⚠️ Empty-diff short-circuit: the dev produced no "
                        "changes on this branch. Routing directly to "
                        f"{StoryState.BLOCKED_REVIEW_NONCONVERGENT.value} "
                        "instead of churning review cycles."
                    )
                except Exception:  # pragma: no cover - real-run path
                    pass
            return HandlerResult(
                next_state=StoryState(story.state), payload=result, error=story.error
            )

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
        rcaps = (
            "## App test capabilities (HONOR when judging test choices)\n\n"
            f"* `e2e_harness_ready`: {str(app_config.gates.e2e_harness_ready).lower()}\n"
            "* If false, this app has NO runnable Playwright/browser harness. Do "
            "NOT require Playwright/E2E and do NOT flag a finding because a smoke/"
            "flow test was written as pytest/httpx instead of Playwright — that "
            "is the CORRECT choice here, and a backend test that covers the "
            "behavior fully satisfies the acceptance criterion. Treat any stray "
            "Playwright config/spec or 'playwright not wired' as NON-blocking "
            "(`low`), never `medium`/`high`.\n\n"
        )
        history_section = _render_reviewer_history_section(story)
        full_prompt = (
            f"{persona_prompt.rstrip()}\n\n"
            "---\n\n"
            "## Context\n\n"
            f"{prelude.rstrip()}\n\n"
            f"{rcaps}"
            + (f"{history_section}\n\n" if history_section else "")
            + "## Story\n\n"
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
            db_path=db,
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

    # Loop-4 programmatic slop gate. The dev now writes its own tests, so a
    # deterministic scan of the changed test files is the backstop against the
    # classic failure mode (tautological / assert-True / mock-only tests that
    # pass before any real implementation). This runs BEFORE the approve check
    # and can veto an LLM "approve": slop is a hard block routed back to dev.
    # Runs on every real review (dry_run unit tests pass dry_run=True, and the
    # helper returns [] defensively when there is no worktree to scan).
    if not dry_run:
        slop_findings = _slop_findings_for_story(story, app_config, software_factory_root)
        if slop_findings:
            verdict = "request_changes"
            score = min(score, 0.3)
            result["verdict"] = "request_changes"
            result["test_quality_score"] = score
            result.setdefault("test_quality_findings", [])
            result["test_quality_findings"].extend(slop_findings)
            result["slop_detector_findings"] = slop_findings
            story.reviewer_result_json = json.dumps(result)

    # Finality drift clamp. At cycle 3+, blocking findings that share NO file
    # location with the previous cycle's findings AND are not marked
    # ``regression: true`` violate the persona's finality rule (new objections
    # to code that was already in front of the reviewer). Rotating fresh
    # objections each cycle is exactly how benchmark t3/t8 burned 6 cycles
    # without shipping. Clamp such findings to ``low`` (non-blocking); if
    # nothing blocking remains and the score clears the bar, the verdict
    # flips to approve. Slop-detector findings are never clamped (they are
    # deterministic, not reviewer drift).
    if verdict != "approve" and story.reviewer_cycles >= 2:
        try:
            history = json.loads(story.reviewer_history_json or "[]")
        except (json.JSONDecodeError, TypeError):
            history = []
        prev = history[-1] if history else None
        if prev:
            prev_locs = {
                _finding_location_file(f)
                for f in (prev.get("findings") or [])
                + (prev.get("test_quality_findings") or [])
            } - {""}
            slop_ids = {
                id(f) for f in (result.get("slop_detector_findings") or [])
            }
            clamped: list[dict[str, Any]] = []
            for f in result.get("findings") or []:
                if not isinstance(f, dict) or id(f) in slop_ids:
                    continue
                blocking = f.get("severity") in ("medium", "high")
                is_regression = bool(f.get("regression"))
                known_site = _finding_location_file(f) in prev_locs
                if blocking and not is_regression and not known_site:
                    f["severity"] = "low"
                    f["finality_clamped"] = True
                    clamped.append(f)
            if clamped:
                still_blocking = any(
                    isinstance(f, dict) and f.get("severity") in ("medium", "high")
                    for f in result.get("findings") or []
                ) or bool(result.get("slop_detector_findings"))
                log_story_event(
                    story.id,
                    "reviewer_finality_clamped",
                    {
                        "cycle": story.reviewer_cycles + 1,
                        "clamped": [
                            {"location": f.get("location"), "what": (f.get("what") or "")[:160]}
                            for f in clamped
                        ],
                        "approved_after_clamp": not still_blocking and score >= 0.7,
                    },
                    software_factory_root=software_factory_root,
                    slug_hint=story.slug,
                )
                if not still_blocking and score >= 0.7:
                    verdict = "approve"
                    result["verdict"] = "approve"
                    result["finality_clamp_applied"] = True
                story.reviewer_result_json = json.dumps(result)

    if verdict == "approve" and score >= 0.7:
        story.state = advance(story, EVENT_REVIEWER_APPROVE).value
    else:
        # Convergence guard based on finding STABILITY. A cycle whose findings
        # differ from the last is progress; only identical findings repeating
        # _MAX_REVIEW_STUCK times is true churn. A hard backstop on total cycles
        # prevents a slowly-mutating loop from running unbounded.
        story.reviewer_cycles += 1
        sig = _findings_signature(result)
        from factory.chain.event_log import read_story_events

        consecutive_same = 1  # this cycle
        for e in reversed(
            read_story_events(
                story.id, software_factory_root=software_factory_root, slug_hint=story.slug
            )
        ):
            if e.get("event") != "reviewer_cycle":
                continue
            if e.get("sig") == sig:
                consecutive_same += 1
            else:
                break
        stuck = consecutive_same >= _MAX_REVIEW_STUCK
        log_story_event(
            story.id,
            "reviewer_cycle",
            {
                "sig": sig,
                "cycle": story.reviewer_cycles,
                "consecutive_same": consecutive_same,
                "score": score,
            },
            software_factory_root=software_factory_root,
            slug_hint=story.slug,
        )
        if stuck or story.reviewer_cycles >= _MAX_REVIEW_CYCLES:
            story.state = advance(story, EVENT_REVIEW_NONCONVERGENT).value
            reason = (
                f"same findings repeated {consecutive_same}x (stuck)"
                if stuck
                else f"hit hard cap of {_MAX_REVIEW_CYCLES} cycles"
            )
            story.error = (
                f"Review did not converge ({reason}); routed to "
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
        else:
            # Loop-4: dev owns BOTH code and tests, so every actionable
            # rejection — code defects AND test-quality/slop findings — routes
            # back to dev. There is no longer a separate test author to route a
            # test-quality finding to. dev's prompt receives both ``findings``
            # and ``test_quality_findings`` (see handle_dev's reviewer_findings
            # plumbing), so it knows what to fix in code and in tests.
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

    # Record this cycle in the review history (post-clamp, so the next
    # cycle's reviewer prompt and drift clamp see what was ACTUALLY ruled).
    _append_reviewer_history(story, result)
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
            db_path=db,
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

    # Vacuous-diff guard. A deliverable whose entire diff is story files —
    # nothing under context/, no prd.md, no code — delivered nothing: the
    # story file is the WORK ORDER, not the work. scan_pr_diff can't catch
    # this (stories/*.md is a canonical path, so a story-file-only diff scans
    # clean — exactly how benchmark t7 "passed" 2026-07-17 with a diff that
    # only added the seeded story file).
    substantive = [
        f for f in files
        if f and not str(f).startswith("stories/")
    ]
    if files and not substantive:
        story.state = advance(story, EVENT_DOCS_ENFORCER_FAIL).value
        story.error = "vacuous diff: only story files changed — no deliverable content"
        payload["vacuous_diff"] = True
        persist_story(story, db)
        log_story_event(
            story.id,
            "vacuous_diff",
            {"files": files[:20]},
            software_factory_root=software_factory_root,
            slug_hint=story.slug,
        )
        return HandlerResult(next_state=StoryState(story.state), payload=payload, error=story.error)

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

    # Open the PR programmatically for BOTH chains. The "separate worker"
    # the TDD path historically deferred to was removed with the test-first
    # machinery in the Loop-4 cleanup — without this, TDD stories reached
    # PR_OPEN with github_pr_number=None and auto-merge (which matches open
    # PRs to stories by number) could never merge them. Observed live
    # 2026-06-11 on story 5, the first Loop-4 story to complete the chain.
    if (
        not dry_run
        and story.github_pr_number is None
        and story.github_branch
    ):
        opened = _open_pr_for_story(story, app_config, software_factory_root)
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


def _open_pr_for_story(
    story: StoryRecord, app_config: AppConfig, software_factory_root: Path
) -> int | None:
    """Push the feature branch and open a PR via the ``gh`` CLI (both chains).

    Returns the PR number on success, ``None`` on any failure. Failures are
    swallowed — the chain still transitions to ``PR_OPEN`` and the operator
    can open the PR by hand from the (already-pushed) branch.

    Uses ``gh`` rather than pygithub because the chain runs locally with
    ``gh auth login`` already configured; no extra token plumbing needed.

    Pushes from the per-story worktree (same one the writing persona ran in)
    rather than the source repo — that's where the branch's HEAD is.
    """
    import subprocess

    target_repo = _writing_worktree(app_config, software_factory_root, story)
    base = app_config.default_branch or "main"
    branch = story.github_branch
    if story.chain_kind == "docs":
        title = f"docs(context): {story.title}"
        summary_line = "Canonical context files added/updated."
    else:
        title = f"feat({story.scope or 'app'}): {story.title}"
        summary_line = (
            "Code + tests written by the dev persona, approved by the reviewer "
            "and docs-enforcer gates."
        )
    body = (
        f"Generated by the factory {story.chain_kind} chain for story "
        f"#{story.github_issue_number}.\n\n"
        f"Branch: `{branch}`\n"
        f"Story slug: `{story.slug}`\n"
        f"Direction: `{story.direction_id}`\n\n"
        f"{summary_line} The end-of-tick auto-merge worker merges this PR "
        f"when its gates pass (factory_settings.yaml::auto_merge)."
    )
    try:
        # Sync with the base before opening the PR. Sibling stories merge
        # continuously, so a branch cut earlier in the day is stale by PR
        # time and its PR arrives conflicted (three operator hand-merges on
        # 2026-06-11). A clean auto-merge here keeps the PR mergeable; on
        # conflict we abort and proceed — the PR opens conflicted and a
        # human resolves it (never silently auto-resolve). Best-effort: a
        # fetch/merge failure must not block PR creation.
        try:
            subprocess.run(
                ["git", "fetch", "origin", base],
                cwd=str(target_repo),
                check=False,
                capture_output=True,
                timeout=60,
            )
            base_merge = subprocess.run(
                ["git", "merge", "--no-edit", f"origin/{base}"],
                cwd=str(target_repo),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if base_merge.returncode != 0:
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=str(target_repo),
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
        except (subprocess.TimeoutExpired, OSError):
            pass

        # Refresh + PRUNE remote-tracking refs before the lease push. If the
        # feature branch was deleted on origin since this worktree last
        # fetched (e.g. an operator closed a conflicted PR with
        # --delete-branch), the stale local tracking ref makes
        # --force-with-lease reject with "stale info" and PR creation
        # silently fails (story 56, 2026-07-18). A plain single-branch fetch
        # does NOT remove the stale ref — only --prune does.
        subprocess.run(
            ["git", "fetch", "--prune", "origin"],
            cwd=str(target_repo),
            check=False,
            capture_output=True,
            timeout=120,
        )
        # Push the branch; gh pr create needs an upstream ref.
        # --force-with-lease: story branches are factory-owned and single-
        # writer, and origin may hold STALE commits from abandoned earlier
        # attempts (pre-rewrite runs pushed work-preservation commits). The
        # locally approved state is authoritative; a plain push gets a
        # non-fast-forward rejection against that stale history (story 5,
        # 2026-06-11). The lease still aborts if origin moved unexpectedly
        # since our last fetch.
        subprocess.run(
            ["git", "push", "--force-with-lease", "-u", "origin", str(branch)],
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
        # A PR may already exist for this branch (e.g. the story re-reached
        # PR_OPEN after a CI-fix / review re-dispatch). `gh pr create` then
        # exits non-zero with "already exists" — but the story genuinely HAS a
        # PR, so RELINK it: look up the existing PR number and return it.
        # Without this, github_pr_number stays None and the auto-merge / CI-fix
        # machinery can never act on the existing PR (it sits orphaned forever).
        stderr = getattr(_push_exc, "stderr", "") or ""
        if isinstance(_push_exc, subprocess.CalledProcessError) and "already exists" in stderr:
            try:
                existing = subprocess.run(
                    [
                        "gh", "pr", "view", str(branch), "--repo", app_config.repo,
                        "--json", "number", "-q", ".number",
                    ],
                    cwd=str(target_repo), capture_output=True, text=True, timeout=30,
                )
                num = (existing.stdout or "").strip()
                if existing.returncode == 0 and num.isdigit():
                    try:
                        from factory.manager.signals import write_git_event as _wge_relink

                        _wge_relink(
                            kind="pr_open",
                            story_id=story.id,
                            pr_number=int(num),
                            result="relinked",
                            software_factory_root=software_factory_root,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return int(num)
            except (subprocess.SubprocessError, OSError):
                pass
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


def _close_issues_on_deploy(
    story: StoryRecord,
    app_config: AppConfig,
    software_factory_root: Path,
    db: Path,
    github_client: Any,
    dry_run: bool,
) -> None:
    """Close a deployed story's GH issue + its direction tracker (when all
    siblings are deployed). Best-effort; never raises. Fixes the accumulation
    of open story/tracker issues for already-shipped work (audit 2026-07-18).

    ``github_client`` is ``None`` on every real call today: the orchestrator's
    ``_invoke_handler`` never passes one to ``handle_deploy``, so this used to
    silently no-op on every deploy (nothing ever closed). When not
    ``dry_run``, self-construct a client via the shared
    ``factory.providers.github.build_github_client`` helper — the same one
    ``factory.cli._ensure_github_client`` uses — rather than requiring the
    caller to thread one through. Missing token is swallowed to a warning:
    bookkeeping must never break deploy.
    """
    if dry_run:
        return
    client = github_client
    if client is None:
        from factory.providers.github import build_github_client

        client = build_github_client()
        if client is None:
            _logger.warning(
                "deploy: no GitHub token available (GITHUB_TOKEN/GH_TOKEN/"
                "gh auth token) — skipping auto-close of story %s issue(s)",
                story.id,
            )
            return
    try:
        from factory.directions.tracker_issue import (
            close_story_issue,
            maybe_close_tracker_issue,
        )

        close_story_issue(story, app_config, client)
        if story.direction_id:
            maybe_close_tracker_issue(
                story.direction_id,
                app_config,
                client,
                software_factory_root=software_factory_root,
                db_path=db,
            )
    except Exception:  # noqa: BLE001 - bookkeeping must never break deploy
        pass


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
        # This early-return short-circuits before the ``deploy_post_merge``
        # call below, so the reachable-but-only-via-that-path close call a
        # few lines down never runs for apps with deploy.enabled=false
        # (e.g. sacrifice) — the story reaches DEPLOYED here but its GH
        # issues never closed (audit 2026-07-18). Close them here too.
        _close_issues_on_deploy(story, app_config, software_factory_root, db, github_client, dry_run)
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
        _close_issues_on_deploy(story, app_config, software_factory_root, db, github_client, dry_run)
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
        # deploy_disabled_in_config still reaches DEPLOYED — the story is done
        # from the chain's perspective, so close its issues too.
        _close_issues_on_deploy(story, app_config, software_factory_root, db, github_client, dry_run)
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
