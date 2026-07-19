"""Tests for the auto-merge gate evaluators.

Each gate gets pass + fail cases driven from fixture PRContext / story
records. Where a gate runs subprocesses, we drive it in ``dry_run=True``
mode so the test never shells out — except the ablation tests, which need a
real checkout + suite to substantiate the opt-in claim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.app_config import AppConfig, AppGatesConfig
from factory.chain.gates import (
    canonical_paths_only,
    docs_current,
    smoke_green,
    tests_green,
    tests_meaningful,
)
from factory.chain.gates.evaluator import (
    ALL_GATE_LABELS,
    LOOP4_REQUIRED_GATE_LABELS,
    PRContext,
    evaluate_all_gates,
    gate_label_for,
    required_gate_labels,
)
from factory.chain.state_machine import StoryRecord, StoryState


@pytest.fixture
def app_cfg_with_commands() -> AppConfig:
    return AppConfig(
        name="x",
        repo="o/r",
        gates=AppGatesConfig(
            lint_command="ruff check .",
            format_check_command="ruff format --check .",
            type_check_command="mypy .",
            coverage_command="pytest --cov-fail-under=70",
        ),
    )


@pytest.fixture
def app_cfg_empty() -> AppConfig:
    return AppConfig(name="x", repo="o/r")


def _story(
    *,
    state: str = StoryState.TESTS_GREEN.value,
    test_plan: dict | None = None,
    tech_writer: dict | None = None,
    smoke_passed: bool | None = None,
) -> StoryRecord:
    return StoryRecord(
        direction_id="002",
        app="sacrifice",
        title="t",
        slug="s",
        scope="backend",
        state=state,
        test_plan_json=json.dumps(test_plan) if test_plan is not None else None,
        tech_writer_result_json=json.dumps(tech_writer) if tech_writer is not None else None,
        smoke_passed=smoke_passed,
    )


# --- gate_label_for ------------------------------------------------------ #


def test_gate_label_for_replaces_underscores() -> None:
    assert gate_label_for("canonical_paths_only") == "canonical-paths-only"


def test_all_gate_labels_complete() -> None:
    """The canonical set of labels matches the project spec (WS1.6 trimmed the
    six vestigial gates)."""
    assert ALL_GATE_LABELS == [
        "tests-green",
        "tests-meaningful",
        "docs-current",
        "canonical-paths-only",
        "smoke-green",
    ]


def test_removed_gate_labels_are_gone() -> None:
    """The six vestigial gates (read unwritten flags / deleted-persona
    payloads) must no longer appear as canonical labels."""
    for removed in [
        "tests-red-first-confirmed",
        "flow-verified",
        "coverage-verified",
        "lint-clean",
        "format-clean",
        "types-clean",
    ]:
        assert removed not in ALL_GATE_LABELS
        assert removed not in required_gate_labels(AppConfig(name="x", repo="o/r"))


def test_removed_gate_modules_are_deleted() -> None:
    """Importing a removed gate module must fail (the files are gone)."""
    import importlib

    for mod in (
        "tests_red_first_confirmed",
        "flow_verified",
        "coverage_verified",
        "lint_clean",
        "format_clean",
        "types_clean",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"factory.chain.gates.{mod}")


# --- tests_green --------------------------------------------------------- #


def test_tests_green_dry_run_uses_ci_state(app_cfg_empty: AppConfig) -> None:
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", ci_state="success")
    r = tests_green.evaluate(pr, app_cfg_empty)
    assert r.passed
    pr_failed = PRContext(pr_number=1, head_sha="a", base_branch="main", ci_state="failure")
    r = tests_green.evaluate(pr_failed, app_cfg_empty)
    assert not r.passed


def test_tests_green_dry_run_falls_back_to_story_state(app_cfg_empty: AppConfig) -> None:
    story = _story(state=StoryState.TESTS_GREEN.value)
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story)
    r = tests_green.evaluate(pr, app_cfg_empty)
    assert r.passed
    assert "dry-run" in r.reason
    assert r.details.get("authoritative") is False


def test_tests_green_fails_when_story_not_yet_green(app_cfg_empty: AppConfig) -> None:
    story = _story(state=StoryState.DEV_IN_PROGRESS.value)
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story)
    r = tests_green.evaluate(pr, app_cfg_empty)
    assert not r.passed


def test_tests_green_real_run_reruns_test_command(tmp_path: Path) -> None:
    """WS1.4: in real-run the gate RE-RUNS the app's test_command and passes
    only on exit 0 — it must not trust recorded story state / ci_state."""
    green_cfg = AppConfig(name="x", repo="o/r", gates=AppGatesConfig(test_command="true"))
    # Story looks 'green' and CI says success — but the authoritative signal is
    # the re-run, which we force red below.
    story = _story(state=StoryState.PR_OPEN.value)
    pr = PRContext(
        pr_number=1,
        head_sha="a",
        base_branch="main",
        story=story,
        repo_root=tmp_path,
        ci_state="success",
        dry_run=False,
    )
    r = tests_green.evaluate(pr, green_cfg)
    assert r.passed and r.details["authoritative"] is True

    red_cfg = AppConfig(name="x", repo="o/r", gates=AppGatesConfig(test_command="false"))
    r_red = tests_green.evaluate(pr, red_cfg)
    assert not r_red.passed, "recorded-green story must still fail when the re-run is red"
    assert r_red.details["authoritative"] is True


def test_tests_green_real_run_no_command_falls_back_to_ci(tmp_path: Path) -> None:
    cfg = AppConfig(name="x", repo="o/r")  # no test_command
    pr = PRContext(
        pr_number=1,
        head_sha="a",
        base_branch="main",
        story=_story(),
        repo_root=tmp_path,
        ci_state="failure",
        dry_run=False,
    )
    r = tests_green.evaluate(pr, cfg)
    assert not r.passed and "ci_state=failure" in r.reason


# --- tests_meaningful ---------------------------------------------------- #


def test_tests_meaningful_passes_on_clean_diff(tmp_path: Path, app_cfg_empty: AppConfig) -> None:
    f = tmp_path / "tests" / "test_real.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        "def test_a():\n    result = compute()\n    assert result == 5\n", encoding="utf-8"
    )
    pr = PRContext(
        pr_number=1,
        head_sha="a",
        base_branch="main",
        files_changed=["tests/test_real.py"],
        repo_root=tmp_path,
    )
    r = tests_meaningful.evaluate(pr, app_cfg_empty)
    assert r.passed, r.reason


def test_tests_meaningful_fails_on_slop_diff(tmp_path: Path, app_cfg_empty: AppConfig) -> None:
    f = tmp_path / "tests" / "test_slop.py"
    f.parent.mkdir(parents=True)
    f.write_text("def test_a():\n    assert True\n", encoding="utf-8")
    pr = PRContext(
        pr_number=1,
        head_sha="a",
        base_branch="main",
        files_changed=["tests/test_slop.py"],
        repo_root=tmp_path,
    )
    r = tests_meaningful.evaluate(pr, app_cfg_empty)
    assert not r.passed
    assert r.details["findings"]


def test_tests_meaningful_mutation_status_skipped_by_default(
    app_cfg_empty: AppConfig,
) -> None:
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", files_changed=[])
    r = tests_meaningful.evaluate(pr, app_cfg_empty)
    assert r.passed
    assert r.details["mutation_status"] == "skipped"


def test_tests_meaningful_ablation_needs_checkout_in_dry_run() -> None:
    """WS1.3: mutation_testing opt-in in dry-run (no checkout to mutate) must
    FAIL — a green here would be false confidence."""
    cfg = AppConfig(name="x", repo="o/r", gates=AppGatesConfig(mutation_testing=True))
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", files_changed=[])
    r = tests_meaningful.evaluate(pr, cfg)
    assert not r.passed
    assert r.details["mutation_status"] == "unrun_dry_run"


def test_tests_meaningful_ablation_needs_test_command(tmp_path: Path) -> None:
    """Opt-in real-run without a test_command cannot run ablation → fail."""
    cfg = AppConfig(name="x", repo="o/r", gates=AppGatesConfig(mutation_testing=True))
    pr = PRContext(
        pr_number=1,
        head_sha="a",
        base_branch="main",
        files_changed=["mod.py"],
        repo_root=tmp_path,
        dry_run=False,
    )
    r = tests_meaningful.evaluate(pr, cfg)
    assert not r.passed
    assert r.details["mutation_status"] == "no_test_command"


def _ablation_repo(tmp_path: Path, *, test_exercises_unused: bool) -> Path:
    """A tiny runnable repo: ``mod.add`` is always exercised; ``mod.unused`` is
    exercised only when ``test_exercises_unused`` is True."""
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")  # puts repo on sys.path
    (tmp_path / "mod.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef unused():\n    return 42\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    body = [
        "from mod import add" + (", unused" if test_exercises_unused else ""),
        "",
        "def test_add():",
        "    assert add(2, 3) == 5",
    ]
    if test_exercises_unused:
        body += ["", "def test_unused():", "    assert unused() == 42"]
    (tmp_path / "tests" / "test_mod.py").write_text("\n".join(body) + "\n", encoding="utf-8")
    return tmp_path


def test_tests_meaningful_ablation_fails_on_unexercised_symbol(tmp_path: Path) -> None:
    """The suite tests ``add`` but never ``unused`` — gutting ``unused`` leaves
    the suite green, so the gate must FAIL naming it."""
    repo = _ablation_repo(tmp_path, test_exercises_unused=False)
    cfg = AppConfig(
        name="x",
        repo="o/r",
        gates=AppGatesConfig(mutation_testing=True, test_command="python -m pytest -q"),
    )
    pr = PRContext(
        pr_number=1,
        head_sha="a",
        base_branch="main",
        files_changed=["mod.py", "tests/test_mod.py"],
        repo_root=repo,
        dry_run=False,
    )
    r = tests_meaningful.evaluate(pr, cfg)
    assert not r.passed, r.reason
    assert r.details["mutation_status"] == "ablation_failed"
    assert any("unused" in s for s in r.details["unexercised"])
    # The exercised symbol must NOT be flagged.
    assert not any("::add" in s for s in r.details["unexercised"])
    # File restored after ablation.
    assert "return 42" in (repo / "mod.py").read_text()


def test_tests_meaningful_ablation_passes_when_symbols_exercised(tmp_path: Path) -> None:
    """When every sampled symbol is exercised, gutting any of them turns the
    suite red, so the gate passes."""
    repo = _ablation_repo(tmp_path, test_exercises_unused=True)
    cfg = AppConfig(
        name="x",
        repo="o/r",
        gates=AppGatesConfig(mutation_testing=True, test_command="python -m pytest -q"),
    )
    pr = PRContext(
        pr_number=1,
        head_sha="a",
        base_branch="main",
        files_changed=["mod.py", "tests/test_mod.py"],
        repo_root=repo,
        dry_run=False,
    )
    r = tests_meaningful.evaluate(pr, cfg)
    assert r.passed, r.reason
    assert r.details["mutation_status"] == "ablation_passed"


def test_changed_public_symbols_skips_test_and_private(tmp_path: Path) -> None:
    (tmp_path / "svc.py").write_text(
        "def public():\n    return 1\n\n\ndef _private():\n    return 2\n\n\n"
        "class Widget:\n    def render(self):\n        return 3\n"
        "    def _helper(self):\n        return 4\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_svc.py").write_text("def test_x():\n    assert True\n", "utf-8")
    syms = tests_meaningful._changed_public_symbols(
        ["svc.py", "tests/test_svc.py"], tmp_path
    )
    quals = {q for _, q in syms}
    assert quals == {"public", "Widget.render"}


# --- docs_current -------------------------------------------------------- #


def test_docs_current_passes_with_updates(app_cfg_empty: AppConfig) -> None:
    story = _story(tech_writer={"context_updates": [{"path": "context/project.md"}]})
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story)
    r = docs_current.evaluate(pr, app_cfg_empty)
    assert r.passed


def test_docs_current_passes_with_no_updates_but_rationale(app_cfg_empty: AppConfig) -> None:
    story = _story(tech_writer={"context_updates": [], "rationale": "No updates needed."})
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story)
    r = docs_current.evaluate(pr, app_cfg_empty)
    assert r.passed


def test_docs_current_fails_with_no_updates_and_no_rationale(app_cfg_empty: AppConfig) -> None:
    story = _story(tech_writer={"context_updates": []})
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story)
    r = docs_current.evaluate(pr, app_cfg_empty)
    assert not r.passed


def test_docs_current_fails_without_tech_writer_result(app_cfg_empty: AppConfig) -> None:
    story = _story(state=StoryState.TECH_WRITER_DONE.value)
    story.tech_writer_result_json = None
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story)
    r = docs_current.evaluate(pr, app_cfg_empty)
    assert not r.passed


# --- canonical_paths_only ----------------------------------------------- #


def test_canonical_paths_only_passes_on_canonical_diff(app_cfg_empty: AppConfig) -> None:
    pr = PRContext(
        pr_number=1,
        head_sha="a",
        base_branch="main",
        files_changed=["context/project.md", "context/modules/payments.md", "src/payments.py"],
    )
    r = canonical_paths_only.evaluate(pr, app_cfg_empty)
    assert r.passed


def test_canonical_paths_only_fails_on_forbidden_diff(app_cfg_empty: AppConfig) -> None:
    pr = PRContext(
        pr_number=1,
        head_sha="a",
        base_branch="main",
        files_changed=["context/decisions/0001-stack.md"],
    )
    r = canonical_paths_only.evaluate(pr, app_cfg_empty)
    assert not r.passed


# --- evaluate_all_gates ------------------------------------------------- #


def test_evaluate_all_gates_returns_every_label(
    tmp_path: Path, app_cfg_with_commands: AppConfig
) -> None:
    """The aggregator runs every gate and returns one result per label."""
    story = _story(
        test_plan={"test_plan": [{"name": "test_a", "key_steps": ["x"]}]},
        tech_writer={"context_updates": [{"path": "context/project.md"}]},
    )
    pr = PRContext(
        pr_number=1,
        head_sha="a",
        base_branch="main",
        story=story,
        repo_root=tmp_path,
        ci_state="success",
        files_changed=["src/foo.py", "tests/test_foo.py"],
        dry_run=True,
    )
    # Provide a fixture test file for the slop scan.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_foo.py").write_text(
        "def test_foo():\n    result = compute()\n    assert result == 5\n",
        encoding="utf-8",
    )
    results = evaluate_all_gates(pr, app_cfg_with_commands)
    assert set(results.keys()) == set(ALL_GATE_LABELS)
    # All gates pass for this happy-path fixture under dry-run.
    failed = [(k, v.reason) for k, v in results.items() if not v.passed]
    assert not failed, f"unexpected gate failures: {failed!r}"


# --- smoke_green (D002 runtime verifier) --------------------------------- #


def _smoke_cfg(*, ready: bool, command: str | None) -> AppConfig:
    return AppConfig(
        name="x",
        repo="o/r",
        gates=AppGatesConfig(smoke_harness_ready=ready, smoke_command=command),
    )


def test_smoke_skips_when_no_harness(app_cfg_empty: AppConfig) -> None:
    """Apps without a declared harness pass (skip) — never a new merge block."""
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=_story())
    r = smoke_green.evaluate(pr, app_cfg_empty)
    assert r.passed and r.label == "smoke-green"
    assert "skipped" in r.reason


def test_smoke_skips_when_ready_but_no_command() -> None:
    """ready=True but no command is still a skip, not a hard fail."""
    cfg = _smoke_cfg(ready=True, command=None)
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=_story())
    r = smoke_green.evaluate(pr, cfg)
    assert r.passed and "skipped" in r.reason


def test_smoke_dry_run_passes_on_recorded_flag() -> None:
    cfg = _smoke_cfg(ready=True, command="docker compose up -d && ./smoke.sh")
    story = _story(smoke_passed=True)
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story, dry_run=True)
    r = smoke_green.evaluate(pr, cfg)
    assert r.passed and "smoke_passed" in r.reason


def test_smoke_dry_run_fails_without_recorded_flag() -> None:
    cfg = _smoke_cfg(ready=True, command="docker compose up -d && ./smoke.sh")
    story = _story(smoke_passed=None)
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story, dry_run=True)
    r = smoke_green.evaluate(pr, cfg)
    assert not r.passed and "no green smoke run" in r.reason


def test_smoke_real_run_reflects_command_exit(tmp_path: Path) -> None:
    cfg = _smoke_cfg(ready=True, command="true")
    pr = PRContext(
        pr_number=1,
        head_sha="a",
        base_branch="main",
        story=_story(),
        repo_root=tmp_path,
        dry_run=False,
    )
    r = smoke_green.evaluate(pr, cfg)
    assert r.passed and r.details["exit_code"] == 0

    cfg_fail = _smoke_cfg(ready=True, command="false")
    pr_fail = PRContext(
        pr_number=1,
        head_sha="a",
        base_branch="main",
        story=_story(),
        repo_root=tmp_path,
        dry_run=False,
    )
    r_fail = smoke_green.evaluate(pr_fail, cfg_fail)
    assert not r_fail.passed and r_fail.details["exit_code"] != 0


# --- required_gate_labels (per-app opt-in) ------------------------------- #


def test_required_gates_unchanged_without_harness(app_cfg_empty: AppConfig) -> None:
    """An app with no smoke harness keeps exactly the base Loop-4 set."""
    assert required_gate_labels(app_cfg_empty) == LOOP4_REQUIRED_GATE_LABELS
    assert "smoke-green" not in required_gate_labels(app_cfg_empty)


def test_required_gates_add_smoke_when_opted_in() -> None:
    cfg = _smoke_cfg(ready=True, command="docker compose up -d && ./smoke.sh")
    labels = required_gate_labels(cfg)
    assert "smoke-green" in labels
    # Base set is preserved, smoke is additive.
    for base in LOOP4_REQUIRED_GATE_LABELS:
        assert base in labels


def test_required_gates_no_smoke_when_ready_but_no_command() -> None:
    cfg = _smoke_cfg(ready=True, command=None)
    assert "smoke-green" not in required_gate_labels(cfg)
