"""Unit tests for ``_query_ci_state`` — the real-CI conclusion query that
replaced the hardcoded ``ci_state="success"`` in the auto-merge worker.

These pin the parsing of ``gh pr checks --required`` output (tab-separated
rows) so a green string literal can never again masquerade as a real CI pass
(the "thinks CI passed then it crashes" regression class). Note gh v2.45's
``gh pr checks`` does NOT support ``--json`` — the query parses rows, and the
"no required checks reported" case MUST map to None, not "success".
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from factory.chain.auto_merge import _query_ci_state

APP = SimpleNamespace(repo="acme/widget")


def _fake_run(stdout: str, *, stderr: str = "", returncode: int = 0, raise_exc=None):
    def _run(cmd, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if raise_exc is not None:
            raise raise_exc
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return _run


def _rows(*pairs: tuple[str, str]) -> str:
    # Mimic gh's tab-separated "<name>\t<status>\t<elapsed>\t<url>" rows.
    return "\n".join(f"{name}\t{status}\t10s\thttps://x/{name}" for name, status in pairs)


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (_rows(("lint", "pass"), ("pytest", "pass")), "success"),
        (_rows(("lint", "pass"), ("pytest", "fail")), "failure"),
        (_rows(("lint", "pass"), ("pytest", "pending")), "pending"),
        (_rows(("lint", "cancel")), "failure"),
        (_rows(("lint", "pass"), ("cov", "skipping")), "success"),
    ],
)
def test_status_reduction(monkeypatch, rows, expected):
    monkeypatch.setattr(subprocess, "run", _fake_run(rows))
    assert _query_ci_state(app_config=APP, pr_number=42) == expected


def test_no_required_checks_is_none_not_success(monkeypatch):
    # THE false-green trap: gh prints this and exits 0 when protection/required
    # checks are absent. Must be None (fall back to recorded flag), never
    # "success" — otherwise an unprotected repo fabricates a green CI signal.
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_run("", stderr="no required checks reported on the 'main' branch", returncode=0),
    )
    assert _query_ci_state(app_config=APP, pr_number=42) is None


def test_no_checks_reported_is_none(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", _fake_run("", stderr="no checks reported on the 'main' branch")
    )
    assert _query_ci_state(app_config=APP, pr_number=42) is None


def test_empty_output_returns_none(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(""))
    assert _query_ci_state(app_config=APP, pr_number=42) is None


def test_placeholder_pr_number_skips_query(monkeypatch):
    called = {"n": 0}

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        called["n"] += 1
        raise AssertionError("should not run gh for placeholder PR")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert _query_ci_state(app_config=APP, pr_number=-5) is None
    assert called["n"] == 0


def test_gh_missing_returns_none(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("", raise_exc=FileNotFoundError()))
    assert _query_ci_state(app_config=APP, pr_number=42) is None


def test_gh_timeout_returns_none(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", _fake_run("", raise_exc=subprocess.TimeoutExpired("gh", 60))
    )
    assert _query_ci_state(app_config=APP, pr_number=42) is None
