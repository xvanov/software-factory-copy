"""Gate: ``tests-meaningful``.

Two layers, both aimed at the same failure mode — tests that are green
without exercising the delivered code:

1. Static slop detection (always on). Runs the slop detector against the
   PR's test diff. Any finding → fail.
2. Ablation / mutation check (opt-in via ``gates.mutation_testing``). For each
   changed public symbol in the PR's non-test ``.py`` files, no-op its body
   and re-run the suite: if the tests STILL pass with the symbol gutted, they
   don't exercise it → the "coverage" is illusory → fail, naming the symbol.

The ablation layer replaces the old placeholder that failed with
"mutation_testing opted-in but no runner wired" — the runner is now wired
(``factory.runner._run_pytest`` under the isolated test env). It only runs in
real-run mode (a checkout to mutate) and only when the app opts in, so apps
without ``mutation_testing`` see no behavior change.
"""

from __future__ import annotations

import ast
from pathlib import Path

from factory.app_config import AppConfig
from factory.chain.gates.evaluator import GateResult, PRContext
from factory.chain.slop_detector import _looks_like_test_file, scan_diff

# Ablation is O(symbols) full test runs; cap the sample so a large PR can't
# blow the merge-evaluation budget. Symbols beyond the cap are reported as
# truncated (not silently ignored).
_MAX_ABLATION_SYMBOLS = 5


def evaluate(pr: PRContext, app_config: AppConfig) -> GateResult:
    label = "tests-meaningful"
    findings = scan_diff(pr.files_changed, repo_root=pr.repo_root)
    findings_dicts = [fnd.as_dict() for fnd in findings]
    if findings:
        return GateResult(
            label=label,
            passed=False,
            reason=f"{len(findings)} slop finding(s)",
            details={"findings": findings_dicts},
        )

    if not app_config.gates.mutation_testing:
        return GateResult(
            label=label,
            passed=True,
            reason="no slop findings",
            details={"mutation_status": "skipped", "findings": []},
        )

    return _ablation_gate(pr, app_config, label)


# --------------------------------------------------------------------------- #
# Ablation / mutation check
# --------------------------------------------------------------------------- #


def _ablation_gate(pr: PRContext, app_config: AppConfig, label: str) -> GateResult:
    """Real ablation: gut each changed public symbol, re-run tests, and fail
    if the suite survives (i.e. the symbol is never exercised)."""
    # Ablation mutates files on disk and re-runs the suite — impossible without
    # a real checkout. In dry-run we cannot substantiate the opt-in claim, so
    # we block rather than pass (a green here would be false confidence).
    if pr.dry_run or pr.repo_root is None:
        return GateResult(
            label=label,
            passed=False,
            reason="mutation_testing opted-in but ablation needs a real checkout (dry-run/no repo_root)",
            details={"mutation_status": "unrun_dry_run", "findings": []},
        )

    test_command = app_config.gates.test_command
    if not test_command:
        return GateResult(
            label=label,
            passed=False,
            reason="mutation_testing opted-in but no test_command configured to run ablation",
            details={"mutation_status": "no_test_command", "findings": []},
        )

    symbols = _changed_public_symbols(pr.files_changed, pr.repo_root)
    if not symbols:
        return GateResult(
            label=label,
            passed=True,
            reason="mutation_testing opted-in; no changed public symbols to ablate",
            details={"mutation_status": "no_symbols", "findings": []},
        )

    truncated = len(symbols) > _MAX_ABLATION_SYMBOLS
    sample = symbols[:_MAX_ABLATION_SYMBOLS]

    unexercised: list[str] = []
    skipped: list[str] = []
    for rel_path, qualname in sample:
        survived = _symbol_survives_ablation(pr.repo_root, rel_path, qualname, test_command)
        if survived is None:
            skipped.append(f"{rel_path}::{qualname}")
        elif survived:
            unexercised.append(f"{rel_path}::{qualname}")

    if unexercised:
        return GateResult(
            label=label,
            passed=False,
            reason=(
                f"{len(unexercised)} delivered symbol(s) not exercised by tests "
                f"(suite still green with body no-op'd): {', '.join(unexercised)}"
            ),
            details={
                "mutation_status": "ablation_failed",
                "unexercised": unexercised,
                "skipped": skipped,
                "truncated": truncated,
                "findings": [],
            },
        )

    return GateResult(
        label=label,
        passed=True,
        reason=(
            f"ablation: all {len(sample) - len(skipped)} sampled symbol(s) exercised by tests"
            + (" (sample truncated)" if truncated else "")
        ),
        details={
            "mutation_status": "ablation_passed",
            "sampled": [f"{p}::{q}" for p, q in sample],
            "skipped": skipped,
            "truncated": truncated,
            "findings": [],
        },
    )


def _changed_public_symbols(
    files_changed: list[str], repo_root: Path
) -> list[tuple[str, str]]:
    """Return ``(relative_path, qualname)`` for every public function/method
    defined in the PR's changed, non-test ``.py`` files.

    ``qualname`` is ``func`` for a module-level function or ``Class.method``
    for a method. Deterministically ordered by (path, source line) so the
    ablation sample is stable across runs.
    """
    out: list[tuple[str, int, str]] = []
    for rel in files_changed:
        if not rel.endswith(".py") or _looks_like_test_file(rel):
            continue
        abs_path = repo_root / rel
        if not abs_path.is_file():
            continue
        try:
            tree = ast.parse(abs_path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _is_public(node.name):
                    out.append((rel, node.lineno, node.name))
            elif isinstance(node, ast.ClassDef) and _is_public(node.name):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_public(
                        sub.name
                    ):
                        out.append((rel, sub.lineno, f"{node.name}.{sub.name}"))
    out.sort(key=lambda t: (t[0], t[1]))
    return [(rel, qual) for rel, _, qual in out]


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def _symbol_survives_ablation(
    repo_root: Path, rel_path: str, qualname: str, test_command: str
) -> bool | None:
    """Gut ``qualname``'s body, run the suite, restore the file.

    Returns ``True`` if the suite still PASSES with the symbol no-op'd (the
    symbol is un-exercised → a finding), ``False`` if the suite fails (the
    symbol IS exercised → good), or ``None`` if the symbol could not be
    mutated (e.g. unparse failure) and was skipped.
    """
    from factory.runner import _run_pytest

    abs_path = repo_root / rel_path
    original = abs_path.read_text(encoding="utf-8")
    mutated = _mutate_source(original, qualname)
    if mutated is None:
        return None
    try:
        abs_path.write_text(mutated, encoding="utf-8")
        passed, _out = _run_pytest(repo_root, test_command=test_command)
    finally:
        abs_path.write_text(original, encoding="utf-8")
    return passed


def _mutate_source(source: str, qualname: str) -> str | None:
    """Return ``source`` with ``qualname``'s body replaced by
    ``raise NotImplementedError`` — or ``None`` if the symbol can't be found /
    the tree can't be round-tripped.

    ``raise`` (rather than ``return None``) makes any invocation during the
    test run propagate loudly: a test that actually drives the symbol will go
    red, so a suite that stays green proves the symbol was never called.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    target = _find_symbol_node(tree, qualname)
    if target is None:
        return None

    noop = ast.Raise(
        exc=ast.Call(func=ast.Name(id="NotImplementedError", ctx=ast.Load()), args=[], keywords=[]),
        cause=None,
    )
    target.body = [noop]
    ast.fix_missing_locations(tree)
    try:
        return ast.unparse(tree)
    except (AttributeError, ValueError):  # pragma: no cover - unparse edge cases
        return None


def _find_symbol_node(
    tree: ast.Module, qualname: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Locate the FunctionDef/AsyncFunctionDef for ``func`` or ``Class.method``."""
    if "." in qualname:
        cls_name, meth_name = qualname.split(".", 1)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == cls_name:
                for sub in node.body:
                    if (
                        isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and sub.name == meth_name
                    ):
                        return sub
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == qualname:
            return node
    return None
