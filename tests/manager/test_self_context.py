"""Tests for factory.manager.self_context — Phase 9.

All LLM calls are mocked; tests are deterministic.

Test inventory
--------------
test_refresh_writes_all_six_modules_when_no_module_specified
    Mock LLM; run refresh; assert all 6 files exist with non-empty content.

test_refresh_single_module_only_writes_that_one
    --module orchestrator writes only orchestrator.md.

test_refresh_logs_to_context_refresh_ndjson
    Confirm one event per module refreshed.

test_refresh_atomic_writes
    Mock LLM to fail mid-module; verify partial file is not left at target path.

test_dry_run_does_not_call_llm
    Confirm dry_run=True never calls text_run.

test_diagnostician_loads_relevant_context_modules_for_proposed_area
    Five variants (one per proposed_area). For each, plant the relevant module
    files + an irrelevant one; run diagnostician with capturing mock; assert
    the prompt contains the relevant module content and does NOT contain the
    irrelevant one (for areas with selective loading).

test_diagnostician_skips_missing_context_modules
    No context modules exist; diagnostician still runs successfully.

test_diagnostician_unknown_area_loads_all_modules
    proposed_area="unknown" → all 6 modules included.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from factory.manager.diagnostician import _pre_load_source
from factory.manager.self_context import (
    ALL_MODULES,
    _context_refresh_event_path,
    refresh_factory_context,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)

_CANNED_MD = "# {module}\n\nThis is a canned context module for {module}.\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_canned_response(module: str) -> str:
    return _CANNED_MD.format(module=module)


def _plant_context_modules(modules_dir: Path, module_names: list[str]) -> None:
    """Write placeholder context modules to modules_dir."""
    modules_dir.mkdir(parents=True, exist_ok=True)
    for name in module_names:
        (modules_dir / f"{name}.md").write_text(
            f"# {name}\n\nContent of {name} context module.\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# refresh_factory_context tests
# ---------------------------------------------------------------------------


def test_refresh_writes_all_six_modules_when_no_module_specified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mock LLM → run refresh → all 6 files exist with non-empty content."""
    llm_call_count = [0]

    def _mock_text_run(persona: str, prompt: str, model_id: str, schema=None, **kwargs: Any) -> str:
        llm_call_count[0] += 1
        # Extract the module name from the prompt to return appropriate content.
        for mod in ALL_MODULES:
            if f"`{mod}`" in prompt:
                return _make_canned_response(mod)
        return "# Generic\n\nGeneric content.\n"

    monkeypatch.setattr("factory.manager.self_context.text_run", _mock_text_run)
    monkeypatch.setattr("factory.manager.self_context._read_persona_prompt", lambda p: "# mock persona")
    monkeypatch.setattr(
        "factory.manager.self_context.route",  # type: ignore[attr-defined]
        lambda *a, **kw: "anthropic/claude-sonnet-4-6",
        raising=False,
    )
    monkeypatch.setattr(
        "factory.manager.self_context.max_output_tokens_for",  # type: ignore[attr-defined]
        lambda *a, **kw: 8192,
        raising=False,
    )

    # Patch model_router at the module level (imported inside refresh_factory_context)
    import factory.model_router as mr
    monkeypatch.setattr(mr, "route", lambda *a, **kw: "anthropic/claude-sonnet-4-6")
    monkeypatch.setattr(mr, "max_output_tokens_for", lambda *a, **kw: 8192)

    result = refresh_factory_context(root=tmp_path)

    assert result["refreshed"] == 6, f"Expected 6 refreshed, got {result}"
    assert result["failed"] == 0, f"Expected 0 failed, got {result}"
    assert llm_call_count[0] == 6, f"Expected 6 LLM calls, got {llm_call_count[0]}"

    modules_dir = tmp_path / "apps" / "factory" / "context" / "modules"
    for mod_name in ALL_MODULES:
        mod_file = modules_dir / f"{mod_name}.md"
        assert mod_file.exists(), f"Module file missing: {mod_file}"
        content = mod_file.read_text(encoding="utf-8")
        assert content.strip(), f"Module file empty: {mod_file}"


def test_refresh_single_module_only_writes_that_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--module orchestrator writes only orchestrator.md."""

    def _mock_text_run(persona: str, prompt: str, model_id: str, schema=None, **kwargs: Any) -> str:
        return "# orchestrator\n\nOrchestrator content.\n"

    monkeypatch.setattr("factory.manager.self_context.text_run", _mock_text_run)
    monkeypatch.setattr("factory.manager.self_context._read_persona_prompt", lambda p: "# mock persona")
    import factory.model_router as mr
    monkeypatch.setattr(mr, "route", lambda *a, **kw: "anthropic/claude-sonnet-4-6")
    monkeypatch.setattr(mr, "max_output_tokens_for", lambda *a, **kw: 8192)

    result = refresh_factory_context(root=tmp_path, module="orchestrator")

    assert result["refreshed"] == 1
    assert result["failed"] == 0

    modules_dir = tmp_path / "apps" / "factory" / "context" / "modules"
    assert (modules_dir / "orchestrator.md").exists()
    # All other module files must NOT exist.
    for mod_name in ALL_MODULES:
        if mod_name != "orchestrator":
            assert not (modules_dir / f"{mod_name}.md").exists(), (
                f"Module {mod_name}.md should not exist but does"
            )


def test_refresh_logs_to_context_refresh_ndjson(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirm one event per module refreshed in context_refresh.ndjson."""
    monkeypatch.setattr(
        "factory.manager.self_context.text_run",
        lambda persona, prompt, model_id, schema=None, **kw: "# content\n",
    )
    monkeypatch.setattr("factory.manager.self_context._read_persona_prompt", lambda p: "# mock")
    import factory.model_router as mr
    monkeypatch.setattr(mr, "route", lambda *a, **kw: "anthropic/claude-sonnet-4-6")
    monkeypatch.setattr(mr, "max_output_tokens_for", lambda *a, **kw: 8192)

    result = refresh_factory_context(root=tmp_path)
    assert result["refreshed"] == 6

    event_path = _context_refresh_event_path(tmp_path)
    assert event_path.exists(), "context_refresh.ndjson must be created"

    lines = [ln for ln in event_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 6, f"Expected 6 event lines, got {len(lines)}"

    events = [json.loads(ln) for ln in lines]
    for ev in events:
        assert ev["event"] == "context_module_refreshed"
        assert "module" in ev
        assert "path" in ev


def test_refresh_atomic_writes_no_partial_file_on_llm_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mock LLM to fail mid-module; verify no partial file left at target path."""
    modules_dir = tmp_path / "apps" / "factory" / "context" / "modules"

    call_count = [0]

    def _mock_text_run(persona: str, prompt: str, model_id: str, schema=None, **kwargs: Any) -> str:
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("simulated LLM failure")
        return "# content\n"

    monkeypatch.setattr("factory.manager.self_context.text_run", _mock_text_run)
    monkeypatch.setattr("factory.manager.self_context._read_persona_prompt", lambda p: "# mock")
    import factory.model_router as mr
    monkeypatch.setattr(mr, "route", lambda *a, **kw: "anthropic/claude-sonnet-4-6")
    monkeypatch.setattr(mr, "max_output_tokens_for", lambda *a, **kw: 8192)

    # Refresh only the first module (orchestrator) to test the failure case.
    result = refresh_factory_context(root=tmp_path, module="orchestrator")

    assert result["failed"] == 1, f"Expected 1 failure, got {result}"
    # The target path must NOT exist (no partial file).
    target = modules_dir / "orchestrator.md"
    assert not target.exists(), f"Partial file must not exist at {target}"
    # Temp files must also be cleaned up.
    tmp_files = list(modules_dir.glob(".orchestrator.md.tmp*")) if modules_dir.exists() else []
    assert not tmp_files, f"Temp files found: {tmp_files}"


def test_dry_run_does_not_call_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run=True must never call text_run."""
    llm_called = [False]

    def _mock_text_run(*args: Any, **kwargs: Any) -> str:
        llm_called[0] = True
        return "# content\n"

    monkeypatch.setattr("factory.manager.self_context.text_run", _mock_text_run)
    monkeypatch.setattr("factory.manager.self_context._read_persona_prompt", lambda p: "# mock")
    import factory.model_router as mr
    monkeypatch.setattr(mr, "route", lambda *a, **kw: "anthropic/claude-sonnet-4-6")
    monkeypatch.setattr(mr, "max_output_tokens_for", lambda *a, **kw: 8192)

    refresh_factory_context(root=tmp_path, dry_run=True)

    assert not llm_called[0], "LLM must NOT be called in dry-run mode"
    # In dry-run, success=True but skipped_reason="dry_run" — no files written.
    modules_dir = tmp_path / "apps" / "factory" / "context" / "modules"
    for mod_name in ALL_MODULES:
        assert not (modules_dir / f"{mod_name}.md").exists(), (
            f"Module file must not be written in dry-run: {mod_name}.md"
        )


def test_unknown_module_name_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid module name returns error result without calling LLM."""
    llm_called = [False]
    monkeypatch.setattr(
        "factory.manager.self_context.text_run",
        lambda *a, **kw: (llm_called.__setitem__(0, True) or "# content"),
    )
    import factory.model_router as mr
    monkeypatch.setattr(mr, "route", lambda *a, **kw: "anthropic/claude-sonnet-4-6")
    monkeypatch.setattr(mr, "max_output_tokens_for", lambda *a, **kw: 8192)

    result = refresh_factory_context(root=tmp_path, module="not-a-real-module")
    assert result["failed"] == 1
    assert not llm_called[0]


# ---------------------------------------------------------------------------
# Diagnostician context-module loading tests
# ---------------------------------------------------------------------------


def _make_factory_dir_with_modules(
    tmp_path: Path, module_names: list[str]
) -> Path:
    """Create a minimal factory dir structure + context modules.

    Returns factory_dir (tmp_path/factory/).
    """
    factory_dir = tmp_path / "factory"
    factory_dir.mkdir(parents=True)
    # Create minimal sub-dirs so _pre_load_source doesn't choke.
    (factory_dir / "personas").mkdir()
    (factory_dir / "chain").mkdir()
    (factory_dir / "manager" / "detectors").mkdir(parents=True)

    # Plant context modules.
    modules_dir = tmp_path / "apps" / "factory" / "context" / "modules"
    _plant_context_modules(modules_dir, module_names)

    return factory_dir


@pytest.mark.parametrize(
    "proposed_area, relevant_modules, irrelevant_module",
    [
        ("prompt", ["personas"], "orchestrator"),
        ("prompt_edit", ["personas"], "dispatch"),
        ("persona_settings", ["personas"], "observability"),
        ("dispatch_code", ["orchestrator", "state-machine", "dispatch"], "personas"),
        ("observability", ["observability", "manager"], "personas"),
    ],
)
def test_diagnostician_loads_relevant_context_modules_for_proposed_area(
    tmp_path: Path,
    proposed_area: str,
    relevant_modules: list[str],
    irrelevant_module: str,
) -> None:
    """For each proposed_area, confirm relevant modules are loaded and
    the irrelevant_module is NOT in the bundle (selective loading)."""
    factory_dir = _make_factory_dir_with_modules(
        tmp_path, relevant_modules + [irrelevant_module]
    )

    bundle = _pre_load_source(proposed_area, factory_dir=factory_dir, root=tmp_path)

    # All relevant modules should appear in the bundle keys.
    bundle_keys = list(bundle.keys())
    for mod_name in relevant_modules:
        key = f"[context-module:{mod_name}]"
        assert key in bundle_keys, (
            f"Expected context-module:{mod_name} in bundle for area={proposed_area!r}. "
            f"Bundle keys: {bundle_keys}"
        )
        assert f"Content of {mod_name}" in bundle[key], (
            f"Content of {mod_name} not in bundle value"
        )

    # The irrelevant module should NOT be in the bundle.
    irrelevant_key = f"[context-module:{irrelevant_module}]"
    assert irrelevant_key not in bundle_keys, (
        f"Irrelevant module {irrelevant_module!r} should NOT be in bundle for "
        f"area={proposed_area!r}. Bundle keys: {bundle_keys}"
    )


def test_diagnostician_skips_missing_context_modules(tmp_path: Path) -> None:
    """No context modules exist → diagnostician runs without them in the bundle."""
    factory_dir = tmp_path / "factory"
    factory_dir.mkdir(parents=True)
    (factory_dir / "personas").mkdir()
    (factory_dir / "chain").mkdir()
    (factory_dir / "manager" / "detectors").mkdir(parents=True)
    # No context modules dir at all.

    bundle = _pre_load_source("observability", factory_dir=factory_dir, root=tmp_path)

    # Should not crash; no context-module keys.
    context_keys = [k for k in bundle.keys() if k.startswith("[context-module:")]
    assert context_keys == [], (
        f"No context modules should be loaded when dir missing. Got: {context_keys}"
    )


def test_diagnostician_unknown_area_loads_all_modules(tmp_path: Path) -> None:
    """proposed_area='unknown' → all 6 modules included."""
    factory_dir = _make_factory_dir_with_modules(tmp_path, ALL_MODULES)

    bundle = _pre_load_source("unknown", factory_dir=factory_dir, root=tmp_path)

    for mod_name in ALL_MODULES:
        key = f"[context-module:{mod_name}]"
        assert key in bundle, (
            f"Module {mod_name!r} should be in bundle for unknown area. "
            f"Bundle keys: {list(bundle.keys())}"
        )


def test_diagnostician_skips_context_modules_for_unknown_area_when_dir_missing(
    tmp_path: Path,
) -> None:
    """If context/modules dir doesn't exist, unknown area still works (no crash)."""
    factory_dir = tmp_path / "factory"
    factory_dir.mkdir(parents=True)
    (factory_dir / "personas").mkdir()

    bundle = _pre_load_source("unknown", factory_dir=factory_dir, root=tmp_path)
    # Should not crash — context-module keys just won't be present.
    context_keys = [k for k in bundle.keys() if k.startswith("[context-module:")]
    assert context_keys == []
