"""compose_context_prelude behavior."""

from __future__ import annotations

from pathlib import Path

from factory.context.loader import compose_context_prelude


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
