"""compose_context_prelude behavior."""

from __future__ import annotations

from pathlib import Path

from factory.context.loader import compose_context_prelude
from factory.directions.parser import (
    Direction,
    MissingDirection,
    parse_direction_dir,
    resolve_direction_chain,
)


def _seed_repo(repo: Path) -> None:
    (repo / "context").mkdir(parents=True, exist_ok=True)
    (repo / "context" / "modules").mkdir(parents=True, exist_ok=True)
    (repo / "context" / "project.md").write_text("# project\nApp identity here.\n")
    (repo / "context" / "navigation.md").write_text(
        "## When working on auth\n"
        "- context/modules/auth.md\n"
        "\n"
        "## When working on payments\n"
        "- context/modules/payments.md\n"
    )
    (repo / "context" / "modules" / "auth.md").write_text("# auth module\nauth-body\n")
    (repo / "context" / "modules" / "payments.md").write_text("# payments\npayments-body\n")


def test_full_prelude_with_task_scope(tmp_path: Path) -> None:
    _seed_repo(tmp_path)
    out = compose_context_prelude(persona="dev", app_repo_path=tmp_path, task_scope="auth")

    assert "# Context for persona: dev" in out
    assert "## context/project.md" in out
    assert "App identity here." in out
    assert "## context/navigation.md" in out
    assert "When working on auth" in out
    # task-scoped section pulled the auth module file content in
    assert "auth-body" in out
    # the unmatched module is NOT pulled in
    assert "payments-body" not in out


def test_no_task_scope_skips_module_pull(tmp_path: Path) -> None:
    _seed_repo(tmp_path)
    out = compose_context_prelude(persona="dev", app_repo_path=tmp_path, task_scope=None)
    assert "auth-body" not in out
    assert "payments-body" not in out
    assert "## context/project.md" in out


def test_missing_files_returns_no_context_notice(tmp_path: Path) -> None:
    out = compose_context_prelude(persona="onboarder", app_repo_path=tmp_path)
    assert "NO CONTEXT AVAILABLE" in out


def test_task_scope_no_match_falls_through(tmp_path: Path) -> None:
    _seed_repo(tmp_path)
    out = compose_context_prelude(
        persona="dev", app_repo_path=tmp_path, task_scope="something-that-doesnt-exist"
    )
    assert "No navigation sections matched" in out


def test_case_insensitive_task_scope(tmp_path: Path) -> None:
    _seed_repo(tmp_path)
    out = compose_context_prelude(persona="dev", app_repo_path=tmp_path, task_scope="AUTH")
    assert "auth-body" in out


# ─── direction chain prelude tests ──────────────────────────────────────


def _seed_direction(
    root: Path,
    id_slug: str,
    *,
    title: str | None = None,
    body: str = "",
    acceptance: list[str] | None = None,
    parent_direction: str | None = None,
) -> Direction:
    import yaml

    base = root / "apps" / "sacrifice" / "directions" / id_slug
    base.mkdir(parents=True)
    fm: dict[str, object] = {
        "title": title or id_slug.replace("-", " ").title(),
        "type": "feature",
        "priority": "p2",
        "explore": False,
        "created_at": "2025-01-01T00:00:00+00:00",
    }
    if parent_direction:
        fm["parent_direction"] = parent_direction
    ac_lines = ""
    if acceptance is not None:
        ac_lines = "\n".join(f"- [ ] {item}" for item in acceptance)
    md = f"""---
{yaml.safe_dump(fm, sort_keys=False).strip()}
---

# {fm['title']}

## Why

{body or 'Because reasons.'}

## Acceptance Criteria

{ac_lines}
"""
    (base / "direction.md").write_text(md, encoding="utf-8")
    return parse_direction_dir("sacrifice", base)


def test_chain_prelude_includes_parent_body(tmp_path: Path) -> None:
    _seed_repo(tmp_path)
    _seed_direction(
        tmp_path, "011-parent", title="Parent direction", body="Parent acceptance content."
    )
    child = _seed_direction(
        tmp_path, "012-iter-on-parent", title="Iteration", parent_direction="011-parent"
    )
    chain = resolve_direction_chain(child, tmp_path)
    assert len(chain) == 2

    out = compose_context_prelude(
        persona="dev",
        app_repo_path=tmp_path,
        direction_chain=chain,
        software_factory_root=tmp_path,
    )
    assert "## Direction chain context" in out
    assert "### Parent direction: 011-parent" in out
    assert "Parent acceptance content" in out


def test_chain_prelude_missing_direction_sentinel(tmp_path: Path) -> None:
    _seed_repo(tmp_path)
    child = _seed_direction(
        tmp_path, "012-iter-on-missing", title="Iteration", parent_direction="999-noexist"
    )
    chain = resolve_direction_chain(child, tmp_path)
    assert len(chain) == 2
    assert isinstance(chain[0], MissingDirection)

    out = compose_context_prelude(
        persona="dev",
        app_repo_path=tmp_path,
        direction_chain=chain,
        software_factory_root=tmp_path,
    )
    assert "### Parent direction: 999-noexist" in out
    assert "_(parent direction not found: 999-noexist)_" in out


def test_no_chain_prelude_when_none_passed(tmp_path: Path) -> None:
    _seed_repo(tmp_path)
    out = compose_context_prelude(persona="dev", app_repo_path=tmp_path)
    assert "Direction chain context" not in out
    assert "## context/project.md" in out
