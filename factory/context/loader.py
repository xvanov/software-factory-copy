"""Compose the per-persona context prelude.

A *context prelude* is a markdown string prepended to every persona's system
prompt before a real run. It contains the app's current-state truth as the
agent should see it: project identity, navigation index, and any task-scoped
module files.

Phase-0 contract:
  * Always read ``<repo>/context/project.md`` and ``<repo>/context/navigation.md``.
  * If ``task_scope`` is provided, match it (case-insensitive substring) against
    the scope_labels parsed out of navigation.md; concatenate every referenced
    file found on disk.
  * If project.md or navigation.md is missing (e.g. Onboarder runs before
    context exists), return a single ``NO CONTEXT AVAILABLE`` notice — the
    caller's persona prompt should already know what to do in that mode.
"""

from __future__ import annotations

from pathlib import Path

from factory.context.navigator import parse_navigation

_NO_CONTEXT_NOTICE = (
    "# Context\n"
    "\n"
    "**NO CONTEXT AVAILABLE.**\n"
    "\n"
    "This app repo has no `context/project.md` and/or no `context/navigation.md` "
    "yet. You are likely the Onboarder persona running on a fresh codebase. "
    "Build context from the code itself; populate `context/project.md`, "
    "`context/navigation.md`, and the rest of the canonical context set on this "
    "run. Subsequent personas will rely on what you write here.\n"
)


def _read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return None


def compose_context_prelude(
    persona: str,
    app_repo_path: Path,
    task_scope: str | None = None,
) -> str:
    """Compose the markdown context prelude for ``persona`` against ``app_repo_path``.

    Returns a single string (terminated with one trailing newline) that callers
    prepend to the persona's system prompt.
    """
    repo = Path(app_repo_path)
    project_md = _read_text(repo / "context" / "project.md")
    navigation_md = _read_text(repo / "context" / "navigation.md")

    if project_md is None or navigation_md is None:
        return _NO_CONTEXT_NOTICE

    parts: list[str] = []
    parts.append(f"# Context for persona: {persona}\n")
    parts.append(
        "_The factory composed this prelude. It is current-state truth. "
        "If something here contradicts your priors, the context wins._\n"
    )
    parts.append("\n## context/project.md\n")
    parts.append(project_md.rstrip() + "\n")
    parts.append("\n## context/navigation.md\n")
    parts.append(navigation_md.rstrip() + "\n")

    if task_scope:
        sections = parse_navigation(navigation_md)
        scope_lower = task_scope.lower()
        matched_paths: list[str] = []
        seen: set[str] = set()
        for label, paths in sections:
            if scope_lower in label.lower():
                for p in paths:
                    if p not in seen:
                        seen.add(p)
                        matched_paths.append(p)

        if matched_paths:
            parts.append(f"\n## Task scope: {task_scope}\n")
            parts.append("_Files referenced by matching navigation sections:_\n\n")
            for rel in matched_paths:
                target = repo / rel
                content = _read_text(target)
                parts.append(f"### {rel}\n")
                if content is None:
                    parts.append(f"_(file referenced in navigation.md but not found: {rel})_\n")
                else:
                    parts.append(content.rstrip() + "\n")
                parts.append("\n")
        else:
            parts.append(
                f"\n## Task scope: {task_scope}\n"
                f"_No navigation sections matched. Use project.md + navigation.md as guidance._\n"
            )

    return "".join(parts).rstrip() + "\n"
