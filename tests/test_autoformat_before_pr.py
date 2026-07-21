"""Pre-PR autoformat — see factory/chain/handlers.py::_autoformat_changed_py_before_pr.

A factory self-edit (PR #57, 2026-07-21) shipped with a trivial ``I001``
unsorted-import + missing-trailing-newline. The chain's pre-merge gates
(tests-green / smoke / staging-clone) do NOT run ``ruff``, so the nit only
surfaced at GitHub's required lint check, which blocked the merge and — because
auto-merge had already been enabled — left the story stranded at
``deploy_pending``. The helper runs ``ruff --fix`` + ``ruff format`` on the
story's OWN changed ``.py`` files and commits the result before the branch is
pushed, so the PR is ruff-clean.

These tests use a real git repo (bare 'origin' + working clone) and real
``ruff`` so the ``git diff origin/<base>...HEAD`` scoping and the actual fix
behavior are exercised, not mocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory.chain.handlers import _autoformat_changed_py_before_pr


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, check=True, timeout=60
    )


def _init_repo_with_origin(app_dir: Path, *, ruff_config: bool = True) -> Path:
    """Working repo pushed to a bare 'origin' remote, so ``origin/main`` is a
    resolvable ref — the same topology the chain uses against a real remote."""
    origin = app_dir.parent / f"{app_dir.name}-origin.git"
    _run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)], cwd=app_dir.parent)
    app_dir.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "--initial-branch=main"], cwd=app_dir)
    _run(["git", "config", "user.email", "t@e.x"], cwd=app_dir)
    _run(["git", "config", "user.name", "T E"], cwd=app_dir)
    if ruff_config:
        # A clean, already-formatted file on main so the branch diff is the
        # only dirty thing the helper can touch.
        # Mirror the factory's real config: isort (I) enabled, so an unsorted
        # import block is a real ``ruff check --fix`` target (that was PR #57's
        # I001 failure).
        (app_dir / "pyproject.toml").write_text(
            '[tool.ruff.lint]\nselect = ["E", "F", "I"]\n', encoding="utf-8"
        )
        (app_dir / "clean.py").write_text('"""Clean."""\n\nX = 1\n', encoding="utf-8")
    else:
        (app_dir / "README.md").write_text("# init\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=app_dir)
    _run(["git", "commit", "-q", "-m", "init"], cwd=app_dir)
    _run(["git", "remote", "add", "origin", str(origin)], cwd=app_dir)
    _run(["git", "push", "-u", "-q", "origin", "main"], cwd=app_dir)
    return app_dir


def _new_branch_with_dirty_file(repo: Path, name: str, rel: str, content: str) -> None:
    _run(["git", "checkout", "-q", "-b", name], cwd=repo)
    (repo / rel).write_text(content, encoding="utf-8")
    _run(["git", "add", rel], cwd=repo)
    _run(["git", "commit", "-q", "-m", f"add {rel}"], cwd=repo)


# A file with an unsorted import block (ruff I001) and no trailing newline —
# exactly the shape of the PR #57 lint failure. ``\n`` omitted at end on purpose.
_DIRTY = '"""Dirty module."""\n\nfrom pathlib import Path\nimport os\n\nP = os.getcwd()\nQ = Path(P)'


def _head_message(repo: Path) -> str:
    return _run(["git", "log", "-1", "--pretty=%s"], cwd=repo).stdout.strip()


def _changed_since_origin(repo: Path) -> list[str]:
    out = _run(["git", "diff", "--name-only", "origin/main...HEAD"], cwd=repo).stdout
    return out.split()


def test_autoformat_fixes_and_commits_changed_file(tmp_path: Path) -> None:
    repo = _init_repo_with_origin(tmp_path / "app")
    _new_branch_with_dirty_file(repo, "story/1-x", "feat.py", _DIRTY)
    head_before = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()

    _autoformat_changed_py_before_pr(repo, "main")

    # A new commit was created by the helper (HEAD advanced) with its label.
    head_after = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    assert head_after != head_before
    assert "autoformat" in _head_message(repo)

    # The file is now ruff-clean (imports sorted, trailing newline present).
    clean = subprocess.run(
        ["uv", "run", "ruff", "check", str(repo / "feat.py")],
        cwd=str(repo), capture_output=True, text=True, timeout=120,
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr
    body = (repo / "feat.py").read_text(encoding="utf-8")
    assert body.endswith("\n")
    assert body.index("import os") < body.index("from pathlib import Path")


def test_autoformat_noop_when_already_clean(tmp_path: Path) -> None:
    repo = _init_repo_with_origin(tmp_path / "app")
    clean = '"""OK."""\n\nimport os\n\nP = os.getcwd()\n'
    _new_branch_with_dirty_file(repo, "story/1-x", "ok.py", clean)
    head_before = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()

    _autoformat_changed_py_before_pr(repo, "main")

    # Nothing to fix → no extra commit.
    assert _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip() == head_before


def test_autoformat_only_touches_changed_files(tmp_path: Path) -> None:
    """A TRACKED, ruff-dirty file that lives on the base (origin/main) but is
    NOT part of the branch diff must be left byte-for-byte untouched — the
    helper is scoped strictly to the branch's own changed files, never a
    whole-repo reformat."""
    # ``legacy.py`` is committed on main (thus part of origin/main) while it is
    # ruff-dirty (unsorted imports, no trailing newline). Because main is not
    # required to be ruff-clean here, this proves the helper never touches a
    # file outside ``origin/main...HEAD`` even when that file IS ruff-dirty.
    legacy = '"""Legacy."""\n\nfrom pathlib import Path\nimport os\n\nL = Path(os.getcwd())'
    repo = tmp_path / "app"
    origin = repo.parent / f"{repo.name}-origin.git"
    _run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)], cwd=tmp_path)
    repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "--initial-branch=main"], cwd=repo)
    _run(["git", "config", "user.email", "t@e.x"], cwd=repo)
    _run(["git", "config", "user.name", "T E"], cwd=repo)
    (repo / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["E", "F", "I"]\n', encoding="utf-8")
    (repo / "legacy.py").write_text(legacy, encoding="utf-8")
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "-q", "-m", "init with dirty legacy"], cwd=repo)
    _run(["git", "remote", "add", "origin", str(origin)], cwd=repo)
    _run(["git", "push", "-u", "-q", "origin", "main"], cwd=repo)

    # Branch changes ONLY feat.py.
    _new_branch_with_dirty_file(repo, "story/1-x", "feat.py", _DIRTY)

    _autoformat_changed_py_before_pr(repo, "main")

    # legacy.py (dirty, on base, not in the branch diff) is byte-for-byte
    # unchanged; feat.py (in the diff) is the only thing formatted.
    assert (repo / "legacy.py").read_text(encoding="utf-8") == legacy
    assert (repo / "feat.py").read_text(encoding="utf-8").endswith("\n")
    assert "feat.py" in _changed_since_origin(repo)


def test_autoformat_does_not_apply_semantic_fixes(tmp_path: Path) -> None:
    """The helper must sort imports + format, but NOT apply semantic ``--fix``
    rules — e.g. it must NOT delete an unused import (F401). A blanket
    ``ruff check --fix`` would remove a side-effect import and could break the
    code (which is not re-tested after this step), re-creating the strand."""
    repo = _init_repo_with_origin(tmp_path / "app")
    # ``sys`` is imported but unused (F401) and the block is unsorted (I001).
    dirty = '"""Has unused import."""\n\nimport sys\nimport os\n\nP = os.getcwd()'
    _new_branch_with_dirty_file(repo, "story/1-x", "feat.py", dirty)

    _autoformat_changed_py_before_pr(repo, "main")

    body = (repo / "feat.py").read_text(encoding="utf-8")
    # Imports were SORTED (os before sys)...
    assert body.index("import os") < body.index("import sys")
    # ...but the unused ``import sys`` was NOT deleted (F401 not applied).
    assert "import sys" in body


def test_autoformat_noop_without_ruff_config(tmp_path: Path) -> None:
    """No [tool.ruff] / ruff.toml → skip entirely (never touch a non-ruff repo)."""
    repo = _init_repo_with_origin(tmp_path / "app", ruff_config=False)
    _new_branch_with_dirty_file(repo, "story/1-x", "feat.py", _DIRTY)
    head_before = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()

    _autoformat_changed_py_before_pr(repo, "main")

    assert _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip() == head_before


def test_autoformat_never_raises_on_bad_repo(tmp_path: Path) -> None:
    """A path that isn't a git repo (or has no origin) must not raise."""
    not_a_repo = tmp_path / "nope"
    not_a_repo.mkdir()
    (not_a_repo / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    # Must return cleanly (best-effort contract), not raise.
    _autoformat_changed_py_before_pr(not_a_repo, "main")
