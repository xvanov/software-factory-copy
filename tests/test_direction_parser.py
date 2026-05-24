"""parse_direction_dir behavior."""

from __future__ import annotations

from pathlib import Path

import yaml

from factory.directions.parser import (
    Direction,
    next_direction_id,
    parse_direction_dir,
)


def _write_direction_md(
    dir_path: Path,
    *,
    title: str,
    type_tag: str | None = "feature",
    explore: bool = False,
    body_extra: str = "",
    acceptance: list[str] | None = None,
) -> None:
    fm = {
        "title": title,
        "type": type_tag or "",
        "priority": "p2",
        "explore": explore,
        "created_at": "2025-01-01T00:00:00+00:00",
    }
    ac_lines = ""
    if acceptance is not None:
        ac_lines = "\n".join(f"- [ ] {item}" for item in acceptance)
    body = f"""---
{yaml.safe_dump(fm, sort_keys=False).strip()}
---

# {title}

## Why

{body_extra or "Because reasons."}

## Acceptance Criteria

{ac_lines}
"""
    (dir_path / "direction.md").write_text(body, encoding="utf-8")


def test_minimal_direction_title_and_why(tmp_path: Path) -> None:
    d = tmp_path / "001-minimal"
    d.mkdir()
    _write_direction_md(d, title="Minimal one", acceptance=["AC one"])
    direction = parse_direction_dir("sacrifice", d)

    assert direction.id == "001"
    assert direction.slug == "minimal"
    assert direction.title == "Minimal one"
    assert direction.type_tag == "feature"
    assert direction.why is not None and "Because reasons" in direction.why
    assert direction.acceptance == ["AC one"]
    assert direction.has_flow is False
    assert direction.has_api_spec is False
    assert direction.explore_tag is False
    assert direction.artifacts_paths == []
    assert direction.status == "created"
    assert isinstance(direction, Direction)


def test_direction_with_flow_md(tmp_path: Path) -> None:
    d = tmp_path / "002-has-flow"
    d.mkdir()
    _write_direction_md(d, title="Has flow")
    (d / "flow.md").write_text("# flow\n1. Click submit\n2. See toast\n", encoding="utf-8")
    direction = parse_direction_dir("sacrifice", d)
    assert direction.has_flow is True
    assert direction.has_api_spec is False


def test_direction_with_api_spec_md(tmp_path: Path) -> None:
    d = tmp_path / "003-has-api"
    d.mkdir()
    _write_direction_md(d, title="Has api")
    (d / "api_spec.md").write_text(
        '# api\n- POST /healthz -> 200 {"status":"ok"}\n', encoding="utf-8"
    )
    direction = parse_direction_dir("sacrifice", d)
    assert direction.has_api_spec is True
    assert direction.has_flow is False


def test_direction_with_artifacts(tmp_path: Path) -> None:
    d = tmp_path / "004-has-art"
    d.mkdir()
    _write_direction_md(d, title="Has art")
    (d / "artifacts").mkdir()
    (d / "artifacts" / "screenshot.png").write_bytes(b"\x89PNG\r\n")
    (d / "artifacts" / "sample.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    direction = parse_direction_dir("sacrifice", d)
    assert len(direction.artifacts_paths) == 2
    names = sorted(p.name for p in direction.artifacts_paths)
    assert names == ["sample.csv", "screenshot.png"]


def test_direction_with_explore_tag(tmp_path: Path) -> None:
    d = tmp_path / "005-explore"
    d.mkdir()
    _write_direction_md(d, title="Explore one", explore=True)
    direction = parse_direction_dir("sacrifice", d)
    assert direction.explore_tag is True


def test_state_yaml_status_read(tmp_path: Path) -> None:
    d = tmp_path / "006-stateful"
    d.mkdir()
    _write_direction_md(d, title="With state")
    (d / "state.yaml").write_text(
        yaml.safe_dump({"status": "pm-validated", "tracker_issue": 42}),
        encoding="utf-8",
    )
    direction = parse_direction_dir("sacrifice", d)
    assert direction.status == "pm-validated"
    assert direction.state["tracker_issue"] == 42


def test_next_direction_id_empty(tmp_path: Path) -> None:
    # No apps/<app>/directions/ at all
    assert next_direction_id("sacrifice", tmp_path) == "001"


def test_next_direction_id_increments(tmp_path: Path) -> None:
    root = tmp_path
    base = root / "apps" / "sacrifice" / "directions"
    base.mkdir(parents=True)
    (base / "001-first").mkdir()
    (base / "002-second").mkdir()
    (base / "010-tenth").mkdir()
    (base / "not-a-direction").mkdir()
    assert next_direction_id("sacrifice", root) == "011"
