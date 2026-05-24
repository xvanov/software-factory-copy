"""Tests for ``factory.backpressure.parser`` helpers added in Phase 3."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.backpressure.parser import (
    extract_acceptance_criteria,
    has_meaningful_api_spec,
    has_meaningful_flow,
)
from factory.backpressure.validator import validate_direction
from factory.directions.parser import parse_direction_dir


def _write_direction(
    root: Path,
    *,
    name: str = "002-add-healthz-endpoint",
    flow: str | None = None,
    api: str | None = None,
    acceptance: list[str] | None = None,
    explore: bool = False,
) -> Path:
    d = root / "apps" / "sacrifice" / "directions" / name
    d.mkdir(parents=True, exist_ok=True)
    ac_block = ""
    if acceptance:
        ac_block = "## Acceptance Criteria\n\n" + "\n".join(f"- {a}" for a in acceptance) + "\n"
    fm = "---\ntitle: thing\n"
    if explore:
        fm += "explore: true\n"
    fm += "---\n\n"
    (d / "direction.md").write_text(f"{fm}# thing\n\n## Why\nbecause.\n\n{ac_block}", "utf-8")
    if flow is not None:
        (d / "flow.md").write_text(flow, "utf-8")
    if api is not None:
        (d / "api_spec.md").write_text(api, "utf-8")
    return d


def test_extract_acceptance_criteria_returns_bullets_verbatim(tmp_path: Path) -> None:
    d = _write_direction(
        tmp_path,
        acceptance=["p95 latency < 200ms", "Returns 200 on success"],
    )
    direction = parse_direction_dir("sacrifice", d)
    ac = extract_acceptance_criteria(direction)
    assert ac == ["p95 latency < 200ms", "Returns 200 on success"]


def test_meaningful_flow_passes_with_two_verb_steps(tmp_path: Path) -> None:
    flow = (
        "# Flow\n\n"
        "1. User taps the `Pledge` button.\n"
        "2. User enters $5 and submits the form.\n"
        "3. User sees a `Pledged $5` toast.\n"
    )
    d = _write_direction(tmp_path, flow=flow)
    direction = parse_direction_dir("sacrifice", d)
    assert has_meaningful_flow(direction)


def test_meaningful_flow_fails_with_no_verbs(tmp_path: Path) -> None:
    flow = "# Flow\n\n1. step one\n2. step two\n"
    d = _write_direction(tmp_path, flow=flow)
    direction = parse_direction_dir("sacrifice", d)
    assert not has_meaningful_flow(direction)


def test_meaningful_flow_fails_with_one_step(tmp_path: Path) -> None:
    flow = "# Flow\n\n1. User taps `Pledge`.\n"
    d = _write_direction(tmp_path, flow=flow)
    direction = parse_direction_dir("sacrifice", d)
    assert not has_meaningful_flow(direction)


def test_meaningful_flow_returns_false_when_no_flow_md(tmp_path: Path) -> None:
    d = _write_direction(tmp_path)  # no flow.md
    direction = parse_direction_dir("sacrifice", d)
    assert not has_meaningful_flow(direction)


def test_meaningful_api_spec_passes(tmp_path: Path) -> None:
    api = "`GET /healthz` -> 200 returns `{version, status}`\n"
    d = _write_direction(tmp_path, api=api)
    direction = parse_direction_dir("sacrifice", d)
    assert has_meaningful_api_spec(direction)


def test_meaningful_api_spec_fails_without_method(tmp_path: Path) -> None:
    api = "/healthz -> returns 200 OK\n"
    d = _write_direction(tmp_path, api=api)
    direction = parse_direction_dir("sacrifice", d)
    assert not has_meaningful_api_spec(direction)


def test_meaningful_api_spec_fails_without_response_code(tmp_path: Path) -> None:
    api = "GET /healthz -> returns the version\n"
    d = _write_direction(tmp_path, api=api)
    direction = parse_direction_dir("sacrifice", d)
    assert not has_meaningful_api_spec(direction)


def test_meaningful_api_spec_fails_without_path(tmp_path: Path) -> None:
    api = "GET healthz returns 200\n"  # no leading slash
    d = _write_direction(tmp_path, api=api)
    direction = parse_direction_dir("sacrifice", d)
    assert not has_meaningful_api_spec(direction)


@pytest.mark.parametrize(
    "flow,api,want_severity",
    [
        # Rich flow + AC -> ok
        ("1. User taps `Pledge`.\n2. User sees `Pledged $5` toast.\n", None, "ok"),
        # Thin flow (steps but no verbs) -> warning (still sufficient via steps).
        ("1. step one\n2. step two\n", None, "warning"),
        # API spec with method+path but no response code -> warning.
        (None, "GET /healthz returns the version\n", "warning"),
        # No artifacts at all -> blocking.
        (None, None, "blocking"),
    ],
)
def test_validator_severity_field(
    tmp_path: Path, flow: str | None, api: str | None, want_severity: str
) -> None:
    d = _write_direction(tmp_path, flow=flow, api=api, acceptance=["AC"])
    direction = parse_direction_dir("sacrifice", d)
    result = validate_direction(direction)
    assert result.severity == want_severity, (
        f"got {result.severity!r} structural_issues={result.structural_issues!r} "
        f"missing={result.missing!r}"
    )
