"""Tests for the 10 auto-merge gate evaluators.

Each gate gets pass + fail cases driven from fixture PRContext / story
records. Where a gate runs subprocesses, we drive it in ``dry_run=True``
mode so the test never shells out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.app_config import AppConfig, AppGatesConfig
from factory.chain.gates import (
    canonical_paths_only,
    coverage_verified,
    docs_current,
    flow_verified,
    format_clean,
    lint_clean,
    tests_green,
    tests_meaningful,
    tests_red_first_confirmed,
    types_clean,
)
from factory.chain.gates.evaluator import (
    ALL_GATE_LABELS,
    PRContext,
    evaluate_all_gates,
    gate_label_for,
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
    test_impl: dict | None = None,
    tech_writer: dict | None = None,
) -> StoryRecord:
    return StoryRecord(
        direction_id="002",
        app="sacrifice",
        title="t",
        slug="s",
        scope="backend",
        state=state,
        test_plan_json=json.dumps(test_plan) if test_plan is not None else None,
        test_implementer_result_json=json.dumps(test_impl) if test_impl is not None else None,
        tech_writer_result_json=json.dumps(tech_writer) if tech_writer is not None else None,
    )


# --- gate_label_for ------------------------------------------------------ #


def test_gate_label_for_replaces_underscores() -> None:
    assert gate_label_for("tests_red_first_confirmed") == "tests-red-first-confirmed"


def test_all_gate_labels_complete() -> None:
    """The canonical set of 10 labels matches the project spec."""
    assert len(ALL_GATE_LABELS) == 10
    for expected in [
        "tests-red-first-confirmed",
        "tests-green",
        "tests-meaningful",
        "flow-verified",
        "coverage-verified",
        "lint-clean",
        "format-clean",
        "types-clean",
        "docs-current",
        "canonical-paths-only",
    ]:
        assert expected in ALL_GATE_LABELS


# --- tests_red_first_confirmed ------------------------------------------- #


def test_tests_red_first_confirmed_passes_when_impl_reported_red(app_cfg_empty: AppConfig) -> None:
    story = _story(test_impl={"exit_code": 1, "slop_detected": False})
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story)
    r = tests_red_first_confirmed.evaluate(pr, app_cfg_empty)
    assert r.passed and r.label == "tests-red-first-confirmed"


def test_tests_red_first_confirmed_fails_when_slop_detected(app_cfg_empty: AppConfig) -> None:
    story = _story(test_impl={"exit_code": 0, "slop_detected": True})
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story)
    r = tests_red_first_confirmed.evaluate(pr, app_cfg_empty)
    assert not r.passed and "slop" in r.reason


def test_tests_red_first_confirmed_fails_when_no_story(app_cfg_empty: AppConfig) -> None:
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=None)
    r = tests_red_first_confirmed.evaluate(pr, app_cfg_empty)
    assert not r.passed


# --- tests_green --------------------------------------------------------- #


def test_tests_green_uses_ci_state(app_cfg_empty: AppConfig) -> None:
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", ci_state="success")
    r = tests_green.evaluate(pr, app_cfg_empty)
    assert r.passed
    pr_failed = PRContext(pr_number=1, head_sha="a", base_branch="main", ci_state="failure")
    r = tests_green.evaluate(pr_failed, app_cfg_empty)
    assert not r.passed


def test_tests_green_falls_back_to_story_state(app_cfg_empty: AppConfig) -> None:
    story = _story(state=StoryState.TESTS_GREEN.value)
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story)
    r = tests_green.evaluate(pr, app_cfg_empty)
    assert r.passed


def test_tests_green_fails_when_story_not_yet_green(app_cfg_empty: AppConfig) -> None:
    story = _story(state=StoryState.DEV_IN_PROGRESS.value)
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story)
    r = tests_green.evaluate(pr, app_cfg_empty)
    assert not r.passed


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


def test_tests_meaningful_fails_when_mutation_opted_in_but_unwired() -> None:
    """P5.0 MEDIUM-3: mutation_testing=true with no runner wired must FAIL
    the gate. Silent pass was misleading — operators read the gate green
    and believed mutation coverage was being checked."""
    cfg = AppConfig(name="x", repo="o/r", gates=AppGatesConfig(mutation_testing=True))
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", files_changed=[])
    r = tests_meaningful.evaluate(pr, cfg)
    assert not r.passed, "mutation_testing opt-in without a runner must fail the gate"
    assert r.reason == "mutation_testing opted-in but no runner wired"
    assert r.details["mutation_status"] == "opted_in_no_runner"


# --- flow_verified ------------------------------------------------------- #


def test_flow_verified_passes_when_test_references_flow(
    tmp_path: Path, app_cfg_empty: AppConfig
) -> None:
    # Set up a direction with flow.md.
    (tmp_path / "apps" / "sacrifice" / "directions" / "002-pledge-button").mkdir(parents=True)
    (tmp_path / "apps" / "sacrifice" / "directions" / "002-pledge-button" / "flow.md").write_text(
        "1. User taps Pledge button.\n2. User submits five dollars.\n", encoding="utf-8"
    )
    story = _story(
        test_plan={
            "test_plan": [
                {
                    "name": "test_pledge_button_submits_amount",
                    "what_it_asserts": "User-facing pledge flow stores the submitted dollars.",
                    "why_meaningful": "If broken, users cannot complete the pledge journey.",
                    "key_steps": ["click pledge", "enter five", "submit"],
                }
            ]
        }
    )
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story, repo_root=tmp_path)
    r = flow_verified.evaluate(pr, app_cfg_empty)
    assert r.passed, r.reason


def test_flow_verified_fails_when_no_test_references_flow(
    tmp_path: Path, app_cfg_empty: AppConfig
) -> None:
    (tmp_path / "apps" / "sacrifice" / "directions" / "002-pledge-button").mkdir(parents=True)
    (tmp_path / "apps" / "sacrifice" / "directions" / "002-pledge-button" / "flow.md").write_text(
        "1. User taps Pledge button.\n2. User submits five dollars.\n", encoding="utf-8"
    )
    story = _story(
        test_plan={
            "test_plan": [
                {
                    "name": "test_serializer_handles_null",
                    "what_it_asserts": "An unrelated serializer behavior.",
                    "why_meaningful": "Robustness of the codec layer.",
                    "key_steps": ["arrange", "act", "assert"],
                }
            ]
        }
    )
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story, repo_root=tmp_path)
    r = flow_verified.evaluate(pr, app_cfg_empty)
    assert not r.passed


def test_flow_verified_passes_vacuously_with_no_flow_or_api(
    tmp_path: Path, app_cfg_empty: AppConfig
) -> None:
    """Exploratory direction with no flow / api spec: vacuous pass."""
    (tmp_path / "apps" / "sacrifice" / "directions" / "002-explore").mkdir(parents=True)
    story = _story(test_plan={"test_plan": [{"name": "test_a"}]})
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", story=story, repo_root=tmp_path)
    r = flow_verified.evaluate(pr, app_cfg_empty)
    assert r.passed


# --- coverage_verified --------------------------------------------------- #


def test_coverage_verified_dry_run_passes_with_command(
    app_cfg_with_commands: AppConfig,
) -> None:
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", dry_run=True)
    r = coverage_verified.evaluate(pr, app_cfg_with_commands)
    assert r.passed
    assert "dry-run" in r.reason


def test_coverage_verified_passes_when_no_command(app_cfg_empty: AppConfig) -> None:
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", dry_run=True)
    r = coverage_verified.evaluate(pr, app_cfg_empty)
    assert r.passed
    assert "no coverage_command" in r.reason


# --- lint / format / types_clean ---------------------------------------- #


def test_lint_clean_dry_run_passes_with_command(app_cfg_with_commands: AppConfig) -> None:
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", dry_run=True)
    r = lint_clean.evaluate(pr, app_cfg_with_commands)
    assert r.passed


def test_format_clean_dry_run_passes_with_command(app_cfg_with_commands: AppConfig) -> None:
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", dry_run=True)
    r = format_clean.evaluate(pr, app_cfg_with_commands)
    assert r.passed


def test_types_clean_dry_run_passes_with_command(app_cfg_with_commands: AppConfig) -> None:
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", dry_run=True)
    r = types_clean.evaluate(pr, app_cfg_with_commands)
    assert r.passed


def test_lint_clean_real_run_no_repo_root_fails(app_cfg_with_commands: AppConfig) -> None:
    pr = PRContext(pr_number=1, head_sha="a", base_branch="main", dry_run=False, repo_root=None)
    r = lint_clean.evaluate(pr, app_cfg_with_commands)
    assert not r.passed


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
    """The aggregator runs all 10 gates and returns one result per label."""
    story = _story(
        test_plan={"test_plan": [{"name": "test_a", "key_steps": ["x"]}]},
        test_impl={"exit_code": 1, "slop_detected": False},
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
