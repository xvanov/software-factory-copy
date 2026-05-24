"""Programmatic slop detector — scans test files for anti-patterns.

The detector is the safety net behind the ``tests-meaningful`` auto-merge
gate. It refuses to advance a PR whose tests are obviously empty:

  * ``assert True`` / ``assert False`` / ``assert 1 == 1`` — trivially
    green/red regardless of code under test.
  * ``assert x == x`` (reflexive) — same variable on both sides.
  * ``expect(x).toBe(x)`` and ``expect(true).toBe(true)`` in JS/TS.
  * ``pytest.raises`` blocks whose body re-raises the same exception they
    expect (the test catches its own throw).
  * "Asserted-on-a-just-assigned value" — ``x = 5; assert x == 5``.
  * "Mock-only assertion" — a test function whose only assertions touch
    ``mock.called`` / ``mock.assert_called*`` but never the subject's
    real return value.

The detector is intentionally precision-biased: false negatives are
preferable to false positives because a wrong rejection bounces a PR
back to Test-Designer, which costs budget. Edge cases the detector
deliberately skips:

  * tests that USE mocks but ALSO assert on real return values are not
    flagged — only mock-call-only tests are slop.
  * ``assert True`` inside a ``# noqa: slop`` comment-marked test is
    intentionally left in (escape hatch; not consumed yet).

This module has zero LLM calls. It is pure programmatic scanning.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# Anti-pattern registries
# --------------------------------------------------------------------------- #

SLOP_REGEXES_PYTHON: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*assert\s+True\s*(#.*)?$"), "assert True"),
    (re.compile(r"^\s*assert\s+False\s*(#.*)?$"), "assert False"),
    (re.compile(r"^\s*assert\s+1\s*==\s*1\s*(#.*)?$"), "assert 1 == 1"),
    (re.compile(r"^\s*assert\s+(\w+)\s*==\s*\1\s*(#.*)?$"), "assert x == x"),
]

SLOP_REGEXES_JS_TS: list[tuple[re.Pattern[str], str]] = [
    # Order matters: literal-keyword anti-patterns are listed first so they
    # take precedence over the generic ``expect(x).toBe(x)`` capture which
    # would also match ``expect(true).toBe(true)``.
    (re.compile(r"expect\(\s*true\s*\)\.toBe\(\s*true\s*\)"), "expect(true).toBe(true)"),
    (re.compile(r"expect\(\s*false\s*\)\.toBe\(\s*false\s*\)"), "expect(false).toBe(false)"),
    (re.compile(r"expect\(\s*1\s*\)\.toBe\(\s*1\s*\)"), "expect(1).toBe(1)"),
    (re.compile(r"expect\(\s*([A-Za-z_$][\w$.]*)\s*\)\.toBe\(\s*\1\s*\)"), "expect(x).toBe(x)"),
]

# Test-file path heuristic (used by ``scan_diff``).
_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|[^/]*\.test\.tsx?$|[^/]*\.spec\.tsx?$)"
)


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass
class SlopFinding:
    """A single anti-pattern hit. ``code_excerpt`` is the offending source line
    (or a small window for AST-detected patterns)."""

    path: str
    line: int
    kind: str
    code_excerpt: str
    why_slop: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "kind": self.kind,
            "code_excerpt": self.code_excerpt,
            "why_slop": self.why_slop,
        }


# --------------------------------------------------------------------------- #
# AST detection (Python only)
# --------------------------------------------------------------------------- #


def _ast_findings_python(path_str: str, source: str) -> list[SlopFinding]:
    """Walk Python source AST for the harder anti-patterns.

    Catches:
      * ``x = literal; assert x == literal``  (assert-on-just-set)
      * ``pytest.raises(X): raise X(...)``    (self-throwing raises)
      * "mock-only" test bodies — test function whose only assertions touch
        ``mock.called`` / ``mock.assert_called*`` and never compare real
        return values.
    """
    out: list[SlopFinding] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Don't fail the gate on a syntactically-broken test file; the
        # downstream pytest gate will catch it.
        return out

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            out.extend(_assert_on_just_set(path_str, node))
            out.extend(_self_throwing_raises(path_str, node))
            out.extend(_mock_only_assertions(path_str, node))
    return out


def _assert_on_just_set(
    path_str: str, fn: ast.FunctionDef | ast.AsyncFunctionDef
) -> list[SlopFinding]:
    out: list[SlopFinding] = []
    body = fn.body
    for i in range(len(body) - 1):
        stmt = body[i]
        next_stmt = body[i + 1]
        if not isinstance(stmt, ast.Assign):
            continue
        if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
            continue
        target_name = stmt.targets[0].id
        rhs = stmt.value
        if not isinstance(next_stmt, ast.Assert):
            continue
        test = next_stmt.test
        if not isinstance(test, ast.Compare):
            continue
        if not isinstance(test.left, ast.Name) or test.left.id != target_name:
            continue
        if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
            continue
        comp = test.comparators[0]
        # Compare values by AST equivalence for literals; or same ``Name`` id.
        if _ast_equiv(rhs, comp):
            out.append(
                SlopFinding(
                    path=path_str,
                    line=next_stmt.lineno,
                    kind="assert_on_just_set",
                    code_excerpt=f"{target_name} = ...; assert {target_name} == <same>",
                    why_slop=(
                        f"Test asserts {target_name!r} equals the literal it was just assigned. "
                        "This tests Python's =, not the code under test."
                    ),
                )
            )
    return out


def _ast_equiv(a: ast.expr, b: ast.expr) -> bool:
    """True if two AST expressions are structurally identical (for slop check).

    Handles ``ast.Constant``, ``ast.Name``, simple ``Tuple``/``List`` /
    ``Dict``. Conservative: returns False for any node it doesn't understand.
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.Constant) and isinstance(b, ast.Constant):
        return a.value == b.value
    if isinstance(a, ast.Name) and isinstance(b, ast.Name):
        return a.id == b.id
    if isinstance(a, (ast.Tuple, ast.List)) and isinstance(b, (ast.Tuple, ast.List)):
        if len(a.elts) != len(b.elts):
            return False
        return all(_ast_equiv(x, y) for x, y in zip(a.elts, b.elts, strict=True))
    if isinstance(a, ast.Dict) and isinstance(b, ast.Dict):
        if len(a.keys) != len(b.keys):
            return False
        keys_match = all(
            (k1 is None and k2 is None)
            or (k1 is not None and k2 is not None and _ast_equiv(k1, k2))
            for k1, k2 in zip(a.keys, b.keys, strict=True)
        )
        if not keys_match:
            return False
        return all(_ast_equiv(v1, v2) for v1, v2 in zip(a.values, b.values, strict=True))
    return False


def _self_throwing_raises(
    path_str: str, fn: ast.FunctionDef | ast.AsyncFunctionDef
) -> list[SlopFinding]:
    """``with pytest.raises(X): raise X(...)`` — the test catches its own throw."""
    out: list[SlopFinding] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            ctx = item.context_expr
            # Match ``pytest.raises(<ExcType>)`` — Call on Attribute "raises".
            if not isinstance(ctx, ast.Call):
                continue
            if not (
                isinstance(ctx.func, ast.Attribute)
                and ctx.func.attr == "raises"
                and isinstance(ctx.func.value, ast.Name)
                and ctx.func.value.id == "pytest"
            ):
                continue
            if not ctx.args:
                continue
            expected_exc = ctx.args[0]
            # Look for ``raise <expected_exc>(...)`` directly in the With body.
            for stmt in node.body:
                if isinstance(stmt, ast.Raise) and stmt.exc is not None:
                    raised = stmt.exc
                    if isinstance(raised, ast.Call):
                        raised = raised.func
                    if _ast_equiv(expected_exc, raised):
                        out.append(
                            SlopFinding(
                                path=path_str,
                                line=stmt.lineno,
                                kind="self_throwing_raises",
                                code_excerpt=(
                                    f"with pytest.raises({ast.unparse(expected_exc)}): "
                                    f"raise {ast.unparse(expected_exc)}(...)"
                                ),
                                why_slop=(
                                    "Test raises the same exception it expects to catch. The "
                                    "code under test is never invoked."
                                ),
                            )
                        )
    return out


def _is_mock_name(name: str) -> bool:
    """Heuristic: variable name starts with ``mock_`` or ends with ``_mock``."""
    return name.startswith("mock_") or name.endswith("_mock") or name == "mock"


def _mock_only_assertions(
    path_str: str, fn: ast.FunctionDef | ast.AsyncFunctionDef
) -> list[SlopFinding]:
    """Flag tests whose ONLY assertions are mock-call assertions.

    Specifically:
      * one or more ``assert <mock>.called`` / ``<mock>.call_count == ...``
        or ``<mock>.assert_called*(...)`` expressions
      * NO assert that compares against a non-mock value (i.e. no real
        outcome verification)

    Tests that mix both mock-call AND real-outcome assertions are NOT
    flagged — that's a legitimate pattern.
    """
    mock_only = True
    saw_assertion = False
    for node in ast.walk(fn):
        # bare assert statements
        if isinstance(node, ast.Assert):
            saw_assertion = True
            if not _is_mock_assertion(node.test):
                mock_only = False
        # mock.assert_called_with(...) is an Expr/Call, not an Assert
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Attribute) and call.func.attr.startswith("assert_called"):
                if isinstance(call.func.value, ast.Name) and _is_mock_name(call.func.value.id):
                    saw_assertion = True
                    # mock-only-ish; do not flip mock_only=False
                else:
                    # assert_called* on something not heuristically a mock — let it pass
                    pass

    if saw_assertion and mock_only:
        # Only emit if there was AT LEAST ONE mock-style assertion (otherwise
        # the test is just a no-op which is caught by other detectors).
        only_mock_call_assertions = any(
            (
                isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Attribute)
                and n.value.func.attr.startswith("assert_called")
            )
            or (isinstance(n, ast.Assert) and _is_mock_assertion(n.test))
            for n in ast.walk(fn)
        )
        if only_mock_call_assertions:
            return [
                SlopFinding(
                    path=path_str,
                    line=fn.lineno,
                    kind="mock_only_assertion",
                    code_excerpt=f"def {fn.name}(...): # only asserts on mock.called / assert_called*",
                    why_slop=(
                        "Test only verifies that a mock was called. It never asserts the real "
                        "outcome of the code under test. Replace with an assertion on the real "
                        "return value or observable side effect."
                    ),
                )
            ]
    return []


def _is_mock_assertion(test: ast.expr) -> bool:
    """``mock.called`` or ``mock.call_count == ...`` shape."""
    if isinstance(test, ast.Attribute):
        if test.attr in {"called", "call_count"}:
            if isinstance(test.value, ast.Name) and _is_mock_name(test.value.id):
                return True
    if isinstance(test, ast.Compare):
        # mock.call_count == N
        return _is_mock_assertion(test.left)
    return False


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def _looks_like_test_file(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path.replace("\\", "/")))


def _language_for(path: str) -> str | None:
    if path.endswith(".py"):
        return "python"
    if path.endswith((".ts", ".tsx", ".js", ".jsx")):
        return "js_ts"
    return None


def scan_file(path: Path, language: str | None = None) -> list[SlopFinding]:
    """Scan a single file for slop. Unknown languages return ``[]``."""
    path_str = str(path)
    try:
        source = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    lang = language or _language_for(path_str)
    if lang is None:
        return []
    out: list[SlopFinding] = []
    if lang == "python":
        for lineno, line in enumerate(source.splitlines(), start=1):
            for pat, kind in SLOP_REGEXES_PYTHON:
                if pat.match(line):
                    out.append(
                        SlopFinding(
                            path=path_str,
                            line=lineno,
                            kind=kind,
                            code_excerpt=line.strip(),
                            why_slop=_WHY_PY.get(kind, "trivially-green test"),
                        )
                    )
                    break
        out.extend(_ast_findings_python(path_str, source))
    elif lang == "js_ts":
        for lineno, line in enumerate(source.splitlines(), start=1):
            for pat, kind in SLOP_REGEXES_JS_TS:
                if pat.search(line):
                    out.append(
                        SlopFinding(
                            path=path_str,
                            line=lineno,
                            kind=kind,
                            code_excerpt=line.strip(),
                            why_slop=_WHY_JS.get(kind, "trivially-green test"),
                        )
                    )
                    break
    return out


_WHY_PY: dict[str, str] = {
    "assert True": "Trivially passes regardless of the code under test.",
    "assert False": "Trivially fails — placeholder, not a real test.",
    "assert 1 == 1": "Tests Python's ==, not the subject. Trivially passes.",
    "assert x == x": "Reflexive equality — same variable on both sides. Trivially passes.",
}
_WHY_JS: dict[str, str] = {
    "expect(x).toBe(x)": "Reflexive equality. Trivially passes.",
    "expect(true).toBe(true)": "Trivially passes regardless of the code under test.",
    "expect(false).toBe(false)": "Trivially passes regardless of the code under test.",
    "expect(1).toBe(1)": "Trivially passes regardless of the code under test.",
}


@dataclass
class PRDiffFile:
    """Minimal representation of a PR file. ``status`` mirrors GH's added/modified/removed."""

    path: str
    status: str = "modified"


def scan_diff(
    files: Iterable[PRDiffFile | str], *, repo_root: Path | None = None
) -> list[SlopFinding]:
    """Scan all test files in a PR's file list.

    Accepts either ``PRDiffFile`` objects or bare path strings (the latter
    is convenient for tests and CLI use). Skips deleted files and non-test
    paths. If ``repo_root`` is set, paths are resolved relative to it; else
    paths are interpreted as-is.
    """
    out: list[SlopFinding] = []
    for entry in files:
        if isinstance(entry, str):
            path_str = entry
            status = "modified"
        else:
            path_str = entry.path
            status = entry.status
        if status == "removed":
            continue
        if not _looks_like_test_file(path_str):
            continue
        path = Path(path_str)
        if repo_root is not None:
            path = repo_root / path
        if not path.is_file():
            continue
        out.extend(scan_file(path))
    return out
