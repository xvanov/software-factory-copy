"""Verify tick() emits tick_start + tick_end to ticks.ndjson."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.chain.orchestrator import tick


@pytest.fixture
def factory_root(tmp_path: Path) -> Path:
    """Minimal factory root usable by tick()."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    apps_dir = tmp_path / "apps" / "testapp"
    apps_dir.mkdir(parents=True)
    (apps_dir / "config.yaml").write_text(
        "name: testapp\nrepo: owner/testapp\n",
        encoding="utf-8",
    )
    (tmp_path / "factory_settings.yaml").write_text(
        "caps:\n  global_concurrent_agents: 4\n  per_repo_concurrent_agents: 2\n"
        "  daily_spend_usd: 100\n  hourly_spend_usd: 20\n",
        encoding="utf-8",
    )
    return tmp_path


def test_tick_emits_tick_start_and_tick_end(factory_root: Path) -> None:
    db = factory_root / "state" / "factory.db"
    # Run tick (no in-flight stories — will complete quickly).
    tick(factory_root, "testapp", dry_run=True, db_path=db)

    ndjson = factory_root / "state" / "events" / "ticks.ndjson"
    assert ndjson.exists(), "ticks.ndjson should have been created"
    lines = [ln for ln in ndjson.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) >= 2, f"expected at least 2 lines (tick_start + tick_end), got {len(lines)}"

    events = [json.loads(ln) for ln in lines]
    event_names = [e["event"] for e in events]
    assert "tick_start" in event_names, "tick_start missing"
    assert "tick_end" in event_names, "tick_end missing"

    tick_end = next(e for e in events if e["event"] == "tick_end")
    assert "duration_s" in tick_end, "tick_end must have duration_s"
    assert tick_end["duration_s"] >= 0
    assert tick_end["app"] == "testapp"
    assert tick_end["success"] is True, "tick_end must carry success=True on a normal tick"
    assert "exception" not in tick_end or tick_end["exception"] is None, (
        "tick_end must not carry an exception on a normal tick"
    )
