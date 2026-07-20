"""Tests for WS4.3 — EARS-driven property-based acceptance oracles.

Covers:

  (1) the EARS parser splits WHEN / GIVEN / THE / SHALL correctly, handles the
      other EARS kinds (ubiquitous / state / unwanted / optional) and AC-id
      prefixes, and falls back to None on non-EARS / malformed / UNTESTABLE
      input (→ example-mode);
  (2) mode selection in build_spec_prompt: an EARS AC produces the property-mode
      block with structured decomposition + @given guidance; a non-EARS AC does
      NOT (example-mode fallback); mixed ACs list only the EARS ones as
      properties while all ACs stay verbatim;
  (3) a property test the property-mode author would emit is well-formed, is
      IMPORTABLE + RUNNABLE in the gate's env (hypothesis present), passes on
      satisfying code, and fails-with-shrink on code that violates the invariant
      even when that code's own unit test is green;
  (4) independence + the existing example-based flow are intact.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory.app_config import AppConfig, AppGatesConfig
from factory.chain.acceptance import acceptance_dir, author_acceptance_test, build_spec_prompt
from factory.chain.ears import (
    EarsClause,
    ears_clauses,
    has_ears,
    is_ears,
    parse_ears,
    split_acs,
)
from factory.chain.gates import acceptance_verified
from factory.chain.gates.evaluator import PRContext
from factory.chain.state_machine import StoryRecord, StoryState
from factory.directions.parser import Direction

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _story(*, story_id: int | None = 7, ref: str | None = None) -> StoryRecord:
    return StoryRecord(
        id=story_id,
        direction_id="002",
        app="sacrifice",
        title="normalize emails",
        slug="normalize-email",
        scope="backend",
        state=StoryState.PR_OPEN.value,
        acceptance_test_ref=ref,
        acceptance_expected=True,
    )


def _direction(tmp_path: Path, acceptance: list[str]) -> Direction:
    d = tmp_path / "dir"
    d.mkdir(parents=True, exist_ok=True)
    return Direction(
        id="002",
        slug="emails",
        title="Email handling",
        type_tag=None,
        why=None,
        has_flow=False,
        has_api_spec=False,
        acceptance=acceptance,
        explore_tag=False,
        artifacts_paths=[],
        app="sacrifice",
        status="pm-validated",
        raw_frontmatter={},
        raw_body="",
        dir_path=d,
    )


def _oracle_cfg(*, on: bool = True, command: str | None = None) -> AppConfig:
    return AppConfig(
        name="sacrifice",
        repo="o/r",
        gates=AppGatesConfig(acceptance_oracle=on, acceptance_test_command=command),
    )


# --------------------------------------------------------------------------- #
# (1) EARS parser
# --------------------------------------------------------------------------- #


def test_parse_event_with_given_and_ac_id() -> None:
    c = parse_ears(
        "AC1.2: WHEN a user submits an email, GIVEN the email contains uppercase, "
        "THE system SHALL store it lowercased"
    )
    assert c is not None
    assert c.kind == "event"
    assert c.ac_id == "AC1.2"
    assert c.trigger == "a user submits an email"
    assert c.precondition == "the email contains uppercase"
    assert c.system == "system"
    assert c.response == "store it lowercased"


def test_parse_event_without_precondition() -> None:
    c = parse_ears("WHEN the goal is missing, THE API SHALL return 404")
    assert c is not None
    assert c.kind == "event"
    assert c.trigger == "the goal is missing"
    assert c.precondition is None
    assert c.system == "API"
    assert c.response == "return 404"


def test_parse_lowercase_the_in_trigger_not_mistaken_for_system() -> None:
    # The lowercase "the" inside the trigger must not be picked as the system —
    # the LAST "THE" (the actor before SHALL) is the system.
    c = parse_ears("WHEN the user logs in, THE server SHALL issue a token")
    assert c is not None
    assert c.trigger == "the user logs in"
    assert c.system == "server"
    assert c.response == "issue a token"


def test_parse_ubiquitous() -> None:
    c = parse_ears("THE system SHALL reject payloads larger than 1MB")
    assert c is not None
    assert c.kind == "ubiquitous"
    assert c.trigger is None
    assert c.system == "system"
    assert c.response == "reject payloads larger than 1MB"


def test_parse_unwanted_if_then() -> None:
    c = parse_ears("IF the token is expired, THEN THE system SHALL return 401")
    assert c is not None
    assert c.kind == "unwanted"
    assert c.trigger == "the token is expired"
    assert c.system == "system"
    assert c.response == "return 401"


def test_parse_state_while_and_optional_where() -> None:
    w = parse_ears("WHILE a charge is pending, THE worker SHALL not double-charge")
    assert w is not None and w.kind == "state" and w.trigger == "a charge is pending"
    o = parse_ears("WHERE 2FA is enabled, THE system SHALL require a code")
    assert o is not None and o.kind == "optional" and o.trigger == "2FA is enabled"


def test_non_ears_returns_none() -> None:
    # No SHALL → ordinary prose → example-mode.
    assert parse_ears("the email is lowercased before storing") is None
    assert parse_ears("returns 404 when the goal is missing") is None
    assert not is_ears("returns 404 when the goal is missing")


def test_malformed_and_untestable_fall_back() -> None:
    assert parse_ears("") is None
    assert parse_ears("   ") is None
    # SHALL with no response is malformed → fall back, never a half-clause.
    assert parse_ears("WHEN x happens, THE system SHALL   ") is None
    # Explicit untestable marker is never a property.
    assert parse_ears("AC3.1: UNTESTABLE-AS-WRITTEN — no threshold given") is None


def test_split_and_aggregate_helpers() -> None:
    acs = [
        "WHEN x, THE system SHALL do y",  # EARS
        "some plain criterion",  # not EARS
        "THE system SHALL be idempotent",  # EARS (ubiquitous)
    ]
    pairs = split_acs(acs)
    assert [c is not None for _, c in pairs] == [True, False, True]
    assert has_ears(acs) is True
    assert has_ears(["plain one", "plain two"]) is False
    clauses = ears_clauses(acs)
    assert len(clauses) == 2
    assert all(isinstance(c, EarsClause) for c in clauses)


# --------------------------------------------------------------------------- #
# (2) mode selection in build_spec_prompt
# --------------------------------------------------------------------------- #


def test_prompt_property_mode_for_ears_ac(tmp_path: Path) -> None:
    d = _direction(
        tmp_path,
        ["AC1.1: WHEN an email is stored, THE system SHALL lowercase it"],
    )
    prompt = build_spec_prompt(_story(), d)
    assert "Property-based testing mode (EARS criteria)" in prompt
    assert "@given" in prompt
    assert "hypothesis" in prompt
    # structured decomposition surfaced to the author
    assert "trigger" in prompt and "an email is stored" in prompt
    assert "invariant to assert" in prompt and "lowercase it" in prompt
    assert "[AC1.1]" in prompt


def test_prompt_example_mode_for_non_ears_ac(tmp_path: Path) -> None:
    d = _direction(tmp_path, ["the email is lowercased before storing"])
    prompt = build_spec_prompt(_story(), d)
    # No SHALL anywhere → no property section → example-mode fallback.
    assert "Property-based testing mode" not in prompt
    assert "@given" not in prompt
    # the verbatim AC is still present for example-based authoring
    assert "the email is lowercased before storing" in prompt


def test_prompt_mixed_lists_only_ears_as_properties(tmp_path: Path) -> None:
    d = _direction(
        tmp_path,
        [
            "the response is valid JSON",  # non-EARS
            "WHEN input is negative, THE system SHALL raise ValueError",  # EARS
        ],
    )
    prompt = build_spec_prompt(_story(), d)
    # property block present, decomposes only the EARS one
    assert "Property-based testing mode (EARS criteria)" in prompt
    assert "raise ValueError" in prompt
    # both ACs still appear verbatim in the criteria block (nothing dropped)
    assert "the response is valid JSON" in prompt
    # the non-EARS AC is NOT decomposed as an invariant
    prop_section = prompt.split("Property-based testing mode")[1]
    assert "the response is valid JSON" not in prop_section


def test_prompt_property_block_is_spec_only(tmp_path: Path) -> None:
    """Independence: the property block is derived purely from the ACs — it
    carries no implementation (no `def `, no code body leaks)."""
    d = _direction(tmp_path, ["WHEN x, THE system SHALL be y"])
    prompt = build_spec_prompt(_story(), d)
    assert "def " not in prompt


# --------------------------------------------------------------------------- #
# (3) a property test the author emits is well-formed, runnable, and catches a
#     violation the dev's own suite hides (with Hypothesis shrinking)
# --------------------------------------------------------------------------- #

# What a property-mode author produces from
# "WHEN an email is stored, THE system SHALL lowercase it": assert the invariant
# over many generated inputs rather than one example.
_PROPERTY_TEST_SRC = (
    "from hypothesis import given\n"
    "from hypothesis import strategies as st\n"
    "from mod import normalize_email\n"
    "\n"
    "@given(st.text())\n"
    "def test_ac1_1_email_always_lowercased(raw):\n"
    "    result = normalize_email(raw)\n"
    "    assert result == result.lower()\n"
)


def _make_app_checkout(repo_root: Path, *, correct: bool) -> None:
    (repo_root / "conftest.py").write_text("", encoding="utf-8")  # repo on sys.path
    if correct:
        impl = "def normalize_email(e):\n    return e.lower()\n"
    else:
        # Only strips — never lowercases. The dev's own example test below asserts
        # exactly this buggy behaviour, so the dev suite is green; the PROPERTY
        # oracle is what catches it (and shrinks to a minimal uppercase input).
        impl = "def normalize_email(e):\n    return e.strip()\n"
    (repo_root / "mod.py").write_text(impl, encoding="utf-8")
    (repo_root / "tests").mkdir(exist_ok=True)
    dev_assert = "'user@example.com'" if correct else "'User@Example.COM'"
    (repo_root / "tests" / "test_dev_own.py").write_text(
        "from mod import normalize_email\n"
        "def test_dev():\n"
        f"    assert normalize_email('User@Example.COM ') == {dev_assert}\n",
        encoding="utf-8",
    )


def _write_stored_property_oracle(root: Path, *, story_id: int) -> str:
    out_dir = acceptance_dir(root, "sacrifice", story_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "test_acceptance.py").write_text(_PROPERTY_TEST_SRC, encoding="utf-8")
    return str((out_dir / "test_acceptance.py").relative_to(root))


def test_property_oracle_passes_on_correct_code(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_app_checkout(repo, correct=True)
    root = tmp_path / "factory"
    ref = _write_stored_property_oracle(root, story_id=7)
    pr = PRContext(
        pr_number=1, head_sha="a", base_branch="main",
        story=_story(story_id=7, ref=ref),
        repo_root=repo, software_factory_root=root, dry_run=False,
    )
    r = acceptance_verified.evaluate(pr, _oracle_cfg(on=True))
    assert r.passed, r.reason
    assert r.details["authoritative"] is True


def test_property_oracle_fails_on_violation_even_when_dev_tests_green(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_app_checkout(repo, correct=False)
    # Sanity: the dev's own suite is green on the buggy code.
    dev_run = subprocess.run(
        ["python", "-m", "pytest", "tests/test_dev_own.py", "-q"],
        cwd=repo, capture_output=True, text=True,
    )
    assert dev_run.returncode == 0, dev_run.stdout + dev_run.stderr

    root = tmp_path / "factory"
    ref = _write_stored_property_oracle(root, story_id=7)
    pr = PRContext(
        pr_number=1, head_sha="a", base_branch="main",
        story=_story(story_id=7, ref=ref),
        repo_root=repo, software_factory_root=root, dry_run=False,
    )
    r = acceptance_verified.evaluate(pr, _oracle_cfg(on=True))
    assert not r.passed, "property oracle must fail when the SHALL invariant is violated"
    assert r.details["authoritative"] is True
    # Hypothesis reports a shrunk counterexample (a minimal input) in the output.
    assert "Falsifying example" in r.details["output_tail"]


def test_authored_property_test_is_well_formed_python(tmp_path: Path) -> None:
    """A property test authored (via a fake, no-LLM author_fn) is stored and is
    well-formed: valid Python whose AST has a @given-decorated test function.
    (End-to-end runnability against a real checkout is proven by the gate tests
    above, which need the app module on the path.)"""
    import ast

    story = _story(story_id=42)
    d = _direction(tmp_path, ["WHEN an email is stored, THE system SHALL lowercase it"])
    root = tmp_path / "factory"
    ref = author_acceptance_test(
        story, d, _oracle_cfg(on=True), root,
        dry_run=False, db_path=root / "state" / "factory.db",
        author_fn=lambda _spec, _s: _PROPERTY_TEST_SRC,
    )
    assert ref is not None
    stored = root / ref
    tree = ast.parse(stored.read_text(encoding="utf-8"))  # valid Python or raises
    given_tests = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and any(
            (isinstance(dec, ast.Call) and getattr(dec.func, "id", None) == "given")
            or getattr(dec, "id", None) == "given"
            for dec in node.decorator_list
        )
    ]
    assert given_tests, "expected a @given-decorated test function in the property oracle"


def test_author_receives_property_prompt_for_ears(tmp_path: Path) -> None:
    """The author_fn is handed the property-mode prompt when ACs are EARS —
    the mode selection reaches the (dev-blind) author."""
    story = _story(story_id=5)
    d = _direction(tmp_path, ["WHEN input is empty, THE system SHALL return an error"])
    root = tmp_path / "factory"
    seen: dict[str, str] = {}

    def _capture(spec: str, _s: StoryRecord) -> str:
        seen["spec"] = spec
        return _PROPERTY_TEST_SRC

    author_acceptance_test(
        story, d, _oracle_cfg(on=True), root,
        dry_run=False, db_path=root / "state" / "factory.db", author_fn=_capture,
    )
    assert "Property-based testing mode (EARS criteria)" in seen["spec"]
    assert "@given" in seen["spec"]
