"""Tests for the slop detector.

Meta-note: these tests must themselves avoid the anti-patterns they
detect. Every fixture file is created inside a ``tmp_path`` so the
detector NEVER recursively flags this test file when it is later
scanned in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.chain.slop_detector import (
    PRDiffFile,
    SlopFinding,
    _looks_like_test_file,
    scan_diff,
    scan_file,
)

# --------------------------------------------------------------------------- #
# Path heuristic
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_x.py",
        "backend/tests/test_y.py",
        "test_inline.py",
        "thing_test.py",
        "frontend/src/foo.test.ts",
        "frontend/src/bar.spec.tsx",
    ],
)
def test_looks_like_test_file_recognizes_common_test_paths(path: str) -> None:
    assert _looks_like_test_file(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/module.py",
        "frontend/src/foo.ts",
        "README.md",
        "context/project.md",
    ],
)
def test_looks_like_test_file_rejects_non_test_paths(path: str) -> None:
    assert _looks_like_test_file(path) is False


# --------------------------------------------------------------------------- #
# Python regex anti-patterns
# --------------------------------------------------------------------------- #


def _write_py(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "test_fixture.py"
    f.write_text(body, encoding="utf-8")
    return f


def test_detects_assert_true(tmp_path: Path) -> None:
    f = _write_py(tmp_path, "def test_a():\n    assert True\n")
    findings = scan_file(f)
    assert any(fnd.kind == "assert True" and fnd.line == 2 for fnd in findings), findings


def test_detects_assert_false(tmp_path: Path) -> None:
    f = _write_py(tmp_path, "def test_a():\n    assert False\n")
    findings = scan_file(f)
    assert [fnd.kind for fnd in findings if fnd.kind == "assert False"], findings


def test_detects_assert_1_equals_1(tmp_path: Path) -> None:
    f = _write_py(tmp_path, "def test_a():\n    assert 1 == 1\n")
    findings = scan_file(f)
    assert any(fnd.kind == "assert 1 == 1" for fnd in findings), findings


def test_detects_reflexive_assert_x_eq_x(tmp_path: Path) -> None:
    f = _write_py(tmp_path, "def test_a():\n    x = compute()\n    assert x == x\n")
    findings = scan_file(f)
    assert any(fnd.kind == "assert x == x" for fnd in findings), findings


# --------------------------------------------------------------------------- #
# Python AST anti-patterns
# --------------------------------------------------------------------------- #


def test_detects_assert_on_just_set_literal(tmp_path: Path) -> None:
    """``x = 5; assert x == 5`` — the assertion tests Python's ==, not the SUT."""
    f = _write_py(
        tmp_path,
        "def test_a():\n    x = 5\n    assert x == 5\n",
    )
    findings = scan_file(f)
    kinds = [fnd.kind for fnd in findings]
    assert "assert_on_just_set" in kinds, kinds


def test_detects_assert_on_just_set_string(tmp_path: Path) -> None:
    f = _write_py(
        tmp_path,
        'def test_a():\n    name = "alice"\n    assert name == "alice"\n',
    )
    findings = scan_file(f)
    assert any(fnd.kind == "assert_on_just_set" for fnd in findings), findings


def test_does_not_flag_assert_against_call_result(tmp_path: Path) -> None:
    """``x = compute(); assert x == 5`` is a legitimate test of compute()."""
    f = _write_py(
        tmp_path,
        "def test_a():\n    x = compute()\n    assert x == 5\n",
    )
    findings = scan_file(f)
    assert all(fnd.kind != "assert_on_just_set" for fnd in findings), findings


def test_detects_self_throwing_pytest_raises(tmp_path: Path) -> None:
    """``with pytest.raises(V): raise V()`` — the test catches its own throw."""
    f = _write_py(
        tmp_path,
        "import pytest\n"
        "def test_a():\n"
        "    with pytest.raises(ValueError):\n"
        "        raise ValueError('nope')\n",
    )
    findings = scan_file(f)
    assert any(fnd.kind == "self_throwing_raises" for fnd in findings), findings


def test_does_not_flag_pytest_raises_around_real_call(tmp_path: Path) -> None:
    """``with pytest.raises(V): some_func()`` is legitimate — SUT may raise V."""
    f = _write_py(
        tmp_path,
        "import pytest\ndef test_a():\n    with pytest.raises(ValueError):\n        compute()\n",
    )
    findings = scan_file(f)
    assert all(fnd.kind != "self_throwing_raises" for fnd in findings), findings


def test_detects_mock_only_assertion(tmp_path: Path) -> None:
    """Test that only asserts on ``mock.called`` is slop."""
    f = _write_py(
        tmp_path,
        "def test_a():\n"
        "    service_mock = make_mock()\n"
        "    do_work(service_mock)\n"
        "    assert service_mock.called\n",
    )
    findings = scan_file(f)
    kinds = [fnd.kind for fnd in findings]
    assert "mock_only_assertion" in kinds, kinds


def test_detects_mock_assert_called_with_only(tmp_path: Path) -> None:
    """Test that only calls ``mock.assert_called_with(...)`` is slop."""
    f = _write_py(
        tmp_path,
        "def test_a():\n"
        "    service_mock = make_mock()\n"
        "    do_work(service_mock)\n"
        "    service_mock.assert_called_with(42)\n",
    )
    findings = scan_file(f)
    assert any(fnd.kind == "mock_only_assertion" for fnd in findings), findings


def test_does_not_flag_mock_plus_real_outcome_assertion(tmp_path: Path) -> None:
    """A test that asserts BOTH mock.called AND a real return value is OK."""
    f = _write_py(
        tmp_path,
        "def test_a():\n"
        "    service_mock = make_mock()\n"
        "    result = do_work(service_mock)\n"
        "    assert result == 'expected_outcome'\n"
        "    assert service_mock.called\n",
    )
    findings = scan_file(f)
    assert all(fnd.kind != "mock_only_assertion" for fnd in findings), findings


# --------------------------------------------------------------------------- #
# JS / TS regex anti-patterns
# --------------------------------------------------------------------------- #


def test_detects_expect_x_tobe_x(tmp_path: Path) -> None:
    f = tmp_path / "thing.test.ts"
    f.write_text("it('a', () => { expect(value).toBe(value); });\n", encoding="utf-8")
    findings = scan_file(f)
    assert any(fnd.kind == "expect(x).toBe(x)" for fnd in findings), findings


def test_detects_expect_true_tobe_true(tmp_path: Path) -> None:
    f = tmp_path / "thing.spec.tsx"
    f.write_text("it('a', () => { expect(true).toBe(true); });\n", encoding="utf-8")
    findings = scan_file(f)
    assert any(fnd.kind == "expect(true).toBe(true)" for fnd in findings), findings


def test_js_clean_file_yields_no_findings(tmp_path: Path) -> None:
    f = tmp_path / "thing.test.ts"
    f.write_text(
        "it('renders', () => { expect(render(<App/>).text()).toBe('Hello'); });\n",
        encoding="utf-8",
    )
    findings = scan_file(f)
    assert findings == [], findings


# --------------------------------------------------------------------------- #
# Clean files & multi-pattern files
# --------------------------------------------------------------------------- #


def test_clean_python_test_file_yields_no_findings(tmp_path: Path) -> None:
    """A meaningful test with proper assertions on real return values produces
    zero findings."""
    f = _write_py(
        tmp_path,
        "def test_division_handles_zero():\n    result = divide(10, 2)\n    assert result == 5\n",
    )
    findings = scan_file(f)
    assert findings == [], f"clean file should be clean, got {findings!r}"


def test_multi_pattern_file_emits_all(tmp_path: Path) -> None:
    """A file containing multiple anti-patterns reports each one."""
    f = _write_py(
        tmp_path,
        "def test_a():\n"
        "    assert True\n"
        "def test_b():\n"
        "    x = 1\n"
        "    assert x == 1\n"
        "def test_c():\n"
        "    assert 1 == 1\n",
    )
    findings = scan_file(f)
    kinds = {fnd.kind for fnd in findings}
    assert "assert True" in kinds
    assert "assert 1 == 1" in kinds
    assert "assert_on_just_set" in kinds


# --------------------------------------------------------------------------- #
# scan_diff
# --------------------------------------------------------------------------- #


def test_scan_diff_skips_non_test_files(tmp_path: Path) -> None:
    """Non-test files are not scanned even if they contain ``assert True``."""
    src = tmp_path / "src" / "module.py"
    src.parent.mkdir(parents=True)
    src.write_text("def f():\n    assert True\n", encoding="utf-8")
    findings = scan_diff([str(src)])
    assert findings == [], findings


def test_scan_diff_skips_removed_files(tmp_path: Path) -> None:
    """Deleted files are not scanned (they don't exist on disk anyway)."""
    findings = scan_diff([PRDiffFile(path="tests/test_gone.py", status="removed")])
    assert findings == []


def test_scan_diff_scans_test_files(tmp_path: Path) -> None:
    """Test files in the diff are scanned and findings are aggregated."""
    test_file = tmp_path / "tests" / "test_thing.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_a():\n    assert True\n", encoding="utf-8")
    findings = scan_diff([str(test_file)])
    assert any(fnd.kind == "assert True" for fnd in findings), findings


def test_scan_diff_with_repo_root_resolves_relative_paths(tmp_path: Path) -> None:
    """``repo_root`` resolves relative paths from the GH diff (which are repo-relative)."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_thing.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8"
    )
    findings = scan_diff(["tests/test_thing.py"], repo_root=tmp_path)
    assert any(fnd.kind == "assert True" for fnd in findings), findings


# --------------------------------------------------------------------------- #
# SlopFinding serialization
# --------------------------------------------------------------------------- #


def test_slopfinding_as_dict_carries_all_fields() -> None:
    finding = SlopFinding(
        path="tests/x.py", line=12, kind="assert True", code_excerpt="assert True", why_slop="..."
    )
    d = finding.as_dict()
    assert d == {
        "path": "tests/x.py",
        "line": 12,
        "kind": "assert True",
        "code_excerpt": "assert True",
        "why_slop": "...",
    }
