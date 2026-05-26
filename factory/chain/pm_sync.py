"""PM-sync pipeline — direction → tracker issue.

For each pending direction in ``apps/<app>/directions/``:

1. Parse the directory → ``Direction`` record.
2. Fast pre-check via ``backpressure.validator.validate_direction``.
3. Insufficient → ``record_needs_direction`` + status = ``needs-direction``.
4. Sufficient → invoke PM persona via ``text_run`` with the direction body +
   the canonical context prelude (loader from Phase 0).
5. Persist the PM JSON response under ``pm_result`` in ``state.yaml``.
6. ``open_or_update_tracker_issue`` (idempotent).
7. Status → ``pm-validated`` (Phase 2 spawns child story issues from
   ``pm_result.child_stories``).

``dry_run=True`` skips both the LLM call and the GitHub call; useful for CI
and the acceptance script. In dry-run, ``pm_result`` is computed from a
deterministic fixture based on the parsed direction so the downstream state
shape is exercised.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.app_config import AppConfig, load_app_config, resolve_app_repo_path
from factory.backpressure.validator import ValidationResult, validate_direction
from factory.context.loader import compose_context_prelude
from factory.directions.parser import Direction, MissingDirection, resolve_direction_chain
from factory.directions.tracker_issue import (
    open_or_update_tracker_issue,
    record_needs_direction,
)
from factory.directions.watcher import (
    mark_direction_status,
    merge_state,
    pending_directions,
)
from factory.model_router import route
from factory.runner import _record_run

_PM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "type",
        "priority",
        "has_sufficient_backpressure",
        "missing",
        "tracker_title",
        "tracker_body",
        "child_stories",
        "labels",
        "confidence",
    ],
    "properties": {
        "type": {
            "type": "string",
            "enum": [
                "feature",
                "bug",
                "security",
                "refactor",
                "deploy",
                "chore",
                "infra",
                "ux",
                "docs",
            ],
        },
        "priority": {"type": "string", "enum": ["p0", "p1", "p2", "p3"]},
        "has_sufficient_backpressure": {"type": "boolean"},
        "missing": {"type": "array", "items": {"type": "string"}},
        "tracker_title": {"type": "string"},
        "tracker_body": {"type": "string"},
        "child_stories": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "scope", "rationale"],
                "properties": {
                    "title": {"type": "string"},
                    "scope": {
                        "type": "string",
                        "enum": ["frontend", "backend", "infra", "test", "docs"],
                    },
                    # ``chain_kind`` decides which chain variant runs for the
                    # story: the historical TDD pipeline or the lightweight
                    # docs path. Optional in the schema for backward compat;
                    # the handler defaults missing values to ``"tdd"``.
                    "chain_kind": {"type": "string", "enum": ["tdd", "docs"]},
                    "rationale": {"type": "string"},
                    # Size estimates per the PM persona's hard sizing rule.
                    # Used by ``_validate_story_sizes`` to reject oversized
                    # stories and re-prompt the PM. Optional (legacy PM runs
                    # don't emit them) — when absent, the validator treats
                    # them as 0 (passes) and logs a warning so the operator
                    # can spot under-instrumented decomposition.
                    "estimated_new_files": {"type": "integer", "minimum": 0},
                    "estimated_modified_files": {"type": "integer", "minimum": 0},
                    "estimated_sandbox_iterations": {"type": "integer", "minimum": 0},
                    # Phase 3 EBS: Fibonacci difficulty points the
                    # estimator uses to project ETA. Optional — when
                    # absent, the chain defaults to 3 (median bucket)
                    # and the estimator emits a wider error band.
                    "points": {"type": "integer", "enum": [1, 2, 3, 5, 8, 13]},
                },
            },
        },
        "labels": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
}


@dataclass
class PMSyncSummary:
    processed: int = 0
    validated: int = 0
    needs_direction: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


def _build_pm_prompt(direction: Direction, context_prelude: str) -> str:
    """Construct the user-message prompt for the PM persona text_run."""
    flow_text = ""
    api_text = ""
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

    parts: list[str] = []
    parts.append(context_prelude.rstrip())
    parts.append("\n---\n# Direction\n")
    parts.append(f"id: {direction.id}\nslug: {direction.slug}\napp: {direction.app}\n")
    parts.append(f"frontmatter: {json.dumps(direction.raw_frontmatter, default=str)}\n")
    parts.append("\n## direction.md\n")
    parts.append(direction.raw_body.rstrip())
    if flow_text:
        parts.append("\n\n## flow.md\n")
        parts.append(flow_text.rstrip())
    if api_text:
        parts.append("\n\n## api_spec.md\n")
        parts.append(api_text.rstrip())
    if direction.artifacts_paths:
        parts.append("\n\n## artifacts/ (filenames only)\n")
        for p in direction.artifacts_paths:
            parts.append(f"- {p.name}")
    return "\n".join(parts)


def _dry_run_pm_result(direction: Direction, validation: ValidationResult) -> dict[str, Any]:
    """Deterministic stub PM result for dry-run mode.

    Mirrors the structural validation: if backpressure is sufficient, emits a
    plausible "validated" record; otherwise emits a "needs-direction" record
    with the validator's missing list. No LLM call; no randomness.
    """
    typ = direction.type_tag or "feature"
    priority_default = "p1" if typ in {"security", "bug"} else "p2"
    priority = direction.raw_frontmatter.get("priority") or priority_default

    title_short = direction.title[:60]
    tracker_title = f"[DIRECTION] {title_short}"
    if len(tracker_title) > 69:
        tracker_title = tracker_title[:66] + "..."

    if validation.is_valid:
        child_stories: list[dict[str, str]] = []
        # Docs-typed directions get a single ``chain_kind: "docs"`` story by
        # default — the dry-run fixture mirrors what a live PM call would
        # emit for a context-bootstrap or onboarder-style direction. The
        # docs path skips test_design/test_impl/dev entirely.
        if typ == "docs":
            child_stories.append(
                {
                    "title": title_short,
                    "scope": "docs",
                    "chain_kind": "docs",
                    "rationale": "Documentation-only direction; routed through the docs chain.",
                    "points": 2,
                }
            )
        else:
            if direction.has_api_spec:
                child_stories.append(
                    {
                        "title": f"Implement API: {title_short}",
                        "scope": "backend",
                        "chain_kind": "tdd",
                        "rationale": "Direction declares an API contract; one backend story to implement it.",
                        "points": 3,
                    }
                )
            if direction.has_flow:
                child_stories.append(
                    {
                        "title": f"Implement UI flow: {title_short}",
                        "scope": "frontend",
                        "chain_kind": "tdd",
                        "rationale": "Direction declares a user flow; one frontend story to implement it.",
                        "points": 3,
                    }
                )
        if not child_stories:
            child_stories.append(
                {
                    "title": title_short,
                    "scope": "backend",
                    "chain_kind": "tdd",
                    "rationale": "Explore-tagged direction; single backend story as a starting point.",
                    "points": 3,
                }
            )
        return {
            "type": typ,
            "priority": priority,
            "has_sufficient_backpressure": True,
            "missing": [],
            "tracker_title": tracker_title,
            "tracker_body": (
                f"**{direction.title}**\n\n{(direction.why or '_(no why captured)_').strip()}\n"
            ),
            "child_stories": child_stories,
            "labels": [typ, f"priority/{priority}"],
            "confidence": 0.75,
        }
    return {
        "type": typ,
        "priority": priority,
        "has_sufficient_backpressure": False,
        "missing": validation.missing,
        "tracker_title": tracker_title,
        "tracker_body": (
            f"**{direction.title}**\n\n"
            f"_(needs more backpressure: {', '.join(validation.missing)})_\n"
        ),
        "child_stories": [],
        "labels": [typ, f"priority/{priority}", "needs-direction"],
        "confidence": 0.4,
    }


# --------------------------------------------------------------------------- #
# Story sizing — enforce the PM persona's hard size rule
# --------------------------------------------------------------------------- #


# Per-story size ceilings the chain rejects on. Aligned with the PM persona
# prompt's hard sizing rule; any single child_story exceeding these gets sent
# back to the PM for further decomposition. Picked from observed dev-pass
# budgets: dev sandbox has a 600-iteration default with up to 10 chain
# retries, so a 200-iteration single-story estimate leaves ample headroom.
MAX_NEW_FILES_PER_STORY = 5
MAX_MODIFIED_FILES_PER_STORY = 2
MAX_SANDBOX_ITERATIONS_PER_STORY = 200

# Cap on how many times the chain will re-prompt the PM with size feedback
# before accepting whatever it returns. Each re-prompt is a fresh LLM call;
# small bound keeps cost predictable without giving up after one try.
MAX_PM_REDECOMPOSITION_RETRIES = 3


def _story_size_violations(child_story: dict[str, Any]) -> list[str]:
    """Return a human-readable list of size violations for one child_story.

    Empty list means the story fits. Missing/None estimates are treated as
    zero (assumed-fine) but logged via the returned ``"missing_estimate_..."``
    sentinel so the operator can spot under-instrumented PM output.
    """
    violations: list[str] = []
    nf = child_story.get("estimated_new_files")
    mf = child_story.get("estimated_modified_files")
    it = child_story.get("estimated_sandbox_iterations")
    if nf is None:
        violations.append("missing_estimate_new_files")
    elif isinstance(nf, int) and nf > MAX_NEW_FILES_PER_STORY:
        violations.append(
            f"estimated_new_files={nf} exceeds max {MAX_NEW_FILES_PER_STORY}"
        )
    if mf is None:
        violations.append("missing_estimate_modified_files")
    elif isinstance(mf, int) and mf > MAX_MODIFIED_FILES_PER_STORY:
        violations.append(
            f"estimated_modified_files={mf} exceeds max {MAX_MODIFIED_FILES_PER_STORY}"
        )
    if it is None:
        violations.append("missing_estimate_sandbox_iterations")
    elif isinstance(it, int) and it > MAX_SANDBOX_ITERATIONS_PER_STORY:
        violations.append(
            f"estimated_sandbox_iterations={it} exceeds max "
            f"{MAX_SANDBOX_ITERATIONS_PER_STORY}"
        )
    return violations


def _validate_pm_story_sizes(pm_result: dict[str, Any]) -> dict[int, list[str]]:
    """Return ``{story_index: [violations]}`` for stories that exceed limits.

    Skips when ``has_sufficient_backpressure`` is False (no stories spawn
    anyway) or ``child_stories`` is empty. Missing-estimate sentinels are
    included so the operator can see when PM is emitting unsized output.
    """
    if not pm_result.get("has_sufficient_backpressure"):
        return {}
    out: dict[int, list[str]] = {}
    for idx, story in enumerate(pm_result.get("child_stories") or []):
        violations = _story_size_violations(story)
        # Treat the "missing_estimate_*" sentinels as soft warnings — they
        # don't trigger re-prompts on their own (back-compat with PMs that
        # haven't been updated to emit the fields). Only real-exceed
        # violations gate the re-prompt loop.
        hard = [v for v in violations if not v.startswith("missing_estimate_")]
        if hard:
            out[idx] = hard
    return out


def _format_redecomposition_feedback(violations: dict[int, list[str]]) -> str:
    """Compose the operator-visible feedback string the chain hands back to PM."""
    lines = [
        "## Chain feedback — story sizes exceed dev's per-pass budget",
        "",
        "Your previous decomposition included stories that are too large for a",
        "single dev sandbox pass. The chain rejects oversized stories so dev",
        "doesn't burn retry budget on un-completable slices. Re-decompose the",
        "flagged stories into smaller vertical slices and re-emit the full PM",
        "JSON.",
        "",
        "Per-story size ceilings (HARD):",
        f"  - estimated_new_files ≤ {MAX_NEW_FILES_PER_STORY}",
        f"  - estimated_modified_files ≤ {MAX_MODIFIED_FILES_PER_STORY}",
        f"  - estimated_sandbox_iterations ≤ {MAX_SANDBOX_ITERATIONS_PER_STORY}",
        "",
        "Flagged stories from your last output:",
    ]
    for idx, vs in sorted(violations.items()):
        lines.append(f"  - story[{idx}]: {'; '.join(vs)}")
    lines.append("")
    lines.append(
        "Re-emit the full PM JSON with every child_story now within the limits."
    )
    return "\n".join(lines)


def _call_pm_persona(
    direction: Direction,
    app_repo_path: Path,
    software_factory_root: Path,
) -> dict[str, Any]:
    """Real LLM call. Returns the parsed PM JSON result.

    Loops up to ``MAX_PM_REDECOMPOSITION_RETRIES`` times when the returned
    ``child_stories`` exceed the chain's per-story size ceilings, threading
    structured feedback through to each retry so the PM can correct.
    """
    # Import lazily so dry-run paths don't pull litellm.
    from factory.runner import text_run

    persona = "pm"
    persona_prompt = _read_persona_prompt(persona)

    chain = _resolve_chain_for_direction(direction, software_factory_root)
    context_prelude = compose_context_prelude(
        persona=persona,
        app_repo_path=app_repo_path,
        direction_chain=chain,
        software_factory_root=software_factory_root,
    )
    direction_block = _build_pm_prompt(direction, context_prelude)

    base_prompt = (
        f"{persona_prompt.rstrip()}\n\n"
        "---\n\n"
        "## Input\n\n"
        f"{direction_block}\n\n"
        "---\n\n"
        "Return the JSON object for this direction. No prose outside the JSON."
    )
    model_id = route(persona)

    feedback: str | None = None
    last_result: dict[str, Any] | None = None
    for attempt in range(1, MAX_PM_REDECOMPOSITION_RETRIES + 2):
        # On retries, prepend the chain's structured feedback so the PM sees
        # what failed and re-decomposes accordingly. The persona prompt's
        # sizing rule already covers the policy; this just surfaces which
        # specific stories tripped it.
        full_prompt = base_prompt
        if feedback is not None:
            full_prompt = f"{base_prompt}\n\n---\n\n{feedback}\n"

        # Cap output tokens at 2048 — PM JSON is small.
        result = text_run(
            persona=persona,
            prompt=full_prompt,
            model_id=model_id,
            schema=_PM_SCHEMA,
            max_tokens=2048,
        )
        if not isinstance(result, dict):
            # text_run only returns str when schema is None; treat anything
            # else as failure.
            raise RuntimeError("PM text_run returned a non-dict for schema-mode call")

        last_result = result
        violations = _validate_pm_story_sizes(result)
        if not violations:
            return result
        if attempt > MAX_PM_REDECOMPOSITION_RETRIES:
            break
        feedback = _format_redecomposition_feedback(violations)

    # Out of retries — return the last result with the violations recorded
    # so downstream code can see we tried. Operators surface these via
    # ``factory why`` / the tracker body.
    assert last_result is not None
    last_result.setdefault("_chain_warnings", []).append(
        {
            "kind": "story_sizes_exceeded_after_retries",
            "retries_used": MAX_PM_REDECOMPOSITION_RETRIES,
            "violations": _validate_pm_story_sizes(last_result),
        }
    )
    return last_result


def _resolve_chain_for_direction(
    direction: Direction,
    software_factory_root: Path,
) -> list[Direction | MissingDirection] | None:
    """Resolve the direction chain for ``direction``, returning ``None`` if no
    ``parent_direction`` is set (avoids a useless single-element list)."""
    if not direction.parent_direction:
        return None
    return resolve_direction_chain(direction, software_factory_root)


def _read_persona_prompt(persona: str) -> str:
    from factory.runner import _read_persona_prompt as _rpp

    return _rpp(persona)


def pm_sync(
    app: str,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    github_client: Any = None,
    state_db_path: Path | None = None,
) -> PMSyncSummary:
    """Run the PM-sync pipeline once for ``app``. Returns a summary record."""
    root = Path(software_factory_root)
    db_path = state_db_path or (root / "state" / "factory.db")

    app_config: AppConfig | None = None
    try:
        app_config = load_app_config(app, root)
    except FileNotFoundError as exc:
        # Hard error — without a config we don't know the GitHub repo.
        # Dry-run still works (no GH calls), but real-run cannot.
        if not dry_run:
            raise RuntimeError(f"Cannot run pm-sync without app config: {exc}") from exc

    # In real-run, the GitHub client is required.
    if not dry_run and github_client is None:
        raise RuntimeError(
            "github_client is required for real pm-sync; pass --dry-run for offline use"
        )

    summary = PMSyncSummary()
    pending = pending_directions(app, root, db_path)
    summary.processed = len(pending)

    # App repo path for the context prelude. Phase 7 resolves this via the
    # app's ``config.yaml::app_repo_path`` (default ``../<name>``); on
    # operator-typical layouts that's a sibling of the factory root.
    # If the path doesn't exist on disk yet, the loader returns the
    # NO_CONTEXT_AVAILABLE notice cleanly.
    # ``load_app_config`` may raise FileNotFoundError when the apps/ entry
    # for this app doesn't exist (e.g. test fixtures without a config) —
    # the earlier ``app_config = load_app_config(...)`` block has already
    # handled that case, so reuse the loaded record when available.
    if app_config is not None:
        app_repo_path = resolve_app_repo_path(app_config, root)
    else:
        # Dry-run path with no on-disk config: synthesize a stub.
        app_repo_path = root / "apps" / app

    for direction in pending:
        try:
            validation = validate_direction(direction)

            if not validation.is_valid:
                if dry_run:
                    pm_result = _dry_run_pm_result(direction, validation)
                    _record_run(
                        persona="pm",
                        model=route("pm"),
                        mode="pm-sync-dry-run",
                        tokens_in=0,
                        tokens_out=0,
                        cost_usd=0.0,
                        success=True,
                        story_path=str(direction.dir_path),
                        repo_path="<n/a>",
                        error=None,
                        db_path=db_path,
                    )
                    merge_state(
                        direction,
                        {"pm_result": pm_result, "validation_issues": validation.issues},
                    )
                    mark_direction_status(
                        direction,
                        "needs-direction",
                        by="factory.chain.pm_sync(dry-run)",
                        details={"missing": validation.missing},
                    )
                else:
                    assert app_config is not None and github_client is not None
                    pm_result = _dry_run_pm_result(direction, validation)
                    record_needs_direction(
                        direction,
                        validation.missing,
                        app_config,
                        github_client,
                        pm_result=pm_result,
                    )
                    merge_state(
                        direction,
                        {"pm_result": pm_result, "validation_issues": validation.issues},
                    )
                    mark_direction_status(
                        direction,
                        "needs-direction",
                        by="factory.chain.pm_sync",
                        details={"missing": validation.missing},
                    )
                summary.needs_direction += 1
                continue

            # Backpressure is sufficient — invoke PM persona (or fixture).
            if dry_run:
                pm_result = _dry_run_pm_result(direction, validation)
                _record_run(
                    persona="pm",
                    model=route("pm"),
                    mode="pm-sync-dry-run",
                    tokens_in=0,
                    tokens_out=0,
                    cost_usd=0.0,
                    success=True,
                    story_path=str(direction.dir_path),
                    repo_path="<n/a>",
                    error=None,
                    db_path=db_path,
                )
            else:
                pm_result = _call_pm_persona(direction, app_repo_path, root)

            merge_state(direction, {"pm_result": pm_result})

            if not dry_run:
                assert app_config is not None and github_client is not None
                open_or_update_tracker_issue(
                    direction,
                    app_config,
                    github_client,
                    pm_result=pm_result,
                    software_factory_root=root,
                )

            mark_direction_status(
                direction,
                "pm-validated",
                by="factory.chain.pm_sync" + ("(dry-run)" if dry_run else ""),
                details={"confidence": pm_result.get("confidence")},
            )
            summary.validated += 1

            # Spawn StoryRecord rows for each child_story in pm_result.
            # In dry-run, no GitHub issue is created; story_file_path uses
            # a placeholder issue number of 0. In real-run, the chain
            # opens the GH issue and uses the real number.
            try:
                from factory.chain.handlers import handle_stories_spawned

                if app_config is None and not dry_run:
                    raise RuntimeError("app config required for real-run spawn")
                # In dry-run we still need an AppConfig stub for the handler
                # signature; build a minimal one from the app name if missing.
                spawn_config: AppConfig
                if app_config is not None:
                    spawn_config = app_config
                else:
                    spawn_config = AppConfig(name=app, repo="<dry-run>")
                handle_stories_spawned(
                    direction=direction,
                    pm_result=pm_result,
                    app_config=spawn_config,
                    software_factory_root=root,
                    dry_run=dry_run,
                    db_path=db_path,
                    github_client=github_client,
                )
            except Exception as spawn_exc:
                # Don't fail pm-sync on a spawn error; record it.
                summary.errors.append(
                    (direction.id or direction.slug, f"story-spawn: {spawn_exc!r}")
                )

        except Exception as exc:  # pragma: no cover - exercised in error tests
            summary.errors.append((direction.id or direction.slug, repr(exc)))

    return summary
