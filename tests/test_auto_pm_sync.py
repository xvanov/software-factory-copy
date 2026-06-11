"""``maybe_auto_pm_sync`` — tick-driven triage of pending directions.

Directions filed by scheduled personas (or ``factory tell``) used to rot in
``status: created`` until an operator manually ran ``factory pm-sync``. The
auto-sync hook drains them on every tick, gated by the settings flag and
bounded by the real ``pm_invocations_per_hour`` budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import SQLModel, create_engine

from factory.chain.pm_sync import maybe_auto_pm_sync
from factory.directions.creator import create_direction
from factory.settings.loader import reload_settings


def _seed_app(tmp_path: Path) -> Path:
    apps_dir = tmp_path / "apps" / "sacrifice"
    apps_dir.mkdir(parents=True)
    (apps_dir / "config.yaml").write_text(
        "name: sacrifice\nrepo: xvanov/sacrifice\ndefault_branch: main\n"
        "context_dir: context\ndeploy:\n  enabled: false\nmodels: {}\n",
        encoding="utf-8",
    )
    db = tmp_path / "state" / "factory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(create_engine(f"sqlite:///{db}", echo=False))
    return db


def _seed_pending_direction(tmp_path: Path) -> None:
    create_direction(
        app="sacrifice",
        title="Add healthz endpoint",
        type_tag="feature",
        why="Smoke test wants a stable endpoint.",
        has_ui=False,
        flow_steps=None,
        has_api=True,
        api_spec_lines=['- `POST /healthz` -> 200 {"status":"ok"}'],
        acceptance=["Returns 200", "JSON body has status"],
        explore=False,
        attach_files=None,
        software_factory_root=tmp_path,
    )


def _write_settings(tmp_path: Path, *, enabled: bool, pm_per_hour: int = 4) -> None:
    (tmp_path / "factory_settings.yaml").write_text(
        f"auto_pm_sync:\n  enabled: {str(enabled).lower()}\n"
        f"rate_limits:\n  pm_invocations_per_hour: {pm_per_hour}\n",
        encoding="utf-8",
    )
    reload_settings(tmp_path)


def test_disabled_does_nothing(tmp_path: Path) -> None:
    db = _seed_app(tmp_path)
    _seed_pending_direction(tmp_path)
    _write_settings(tmp_path, enabled=False)

    summary, reason = maybe_auto_pm_sync(
        "sacrifice", tmp_path, dry_run=True, state_db_path=db
    )
    assert summary is None and reason == "disabled"


def test_no_pending_directions_is_a_noop(tmp_path: Path) -> None:
    db = _seed_app(tmp_path)
    _write_settings(tmp_path, enabled=True)

    summary, reason = maybe_auto_pm_sync(
        "sacrifice", tmp_path, dry_run=True, state_db_path=db
    )
    assert summary is None and reason == "no_pending"


def test_pending_direction_is_triaged(tmp_path: Path) -> None:
    db = _seed_app(tmp_path)
    _seed_pending_direction(tmp_path)
    _write_settings(tmp_path, enabled=True)

    summary, reason = maybe_auto_pm_sync(
        "sacrifice", tmp_path, dry_run=True, state_db_path=db
    )
    assert reason == "synced"
    assert summary is not None and summary.processed == 1

    # A second pass finds nothing left to triage.
    summary2, reason2 = maybe_auto_pm_sync(
        "sacrifice", tmp_path, dry_run=True, state_db_path=db
    )
    assert summary2 is None and reason2 == "no_pending"


def test_hourly_pm_budget_blocks_sync(tmp_path: Path) -> None:
    from sqlmodel import Session

    from factory.runner import Run

    db = _seed_app(tmp_path)
    _seed_pending_direction(tmp_path)
    _write_settings(tmp_path, enabled=True, pm_per_hour=2)

    recent = datetime.now(UTC).isoformat()
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with Session(create_engine(f"sqlite:///{db}")) as ses:
        for ts in (recent, recent, old):
            ses.add(Run(ts=ts, persona="pm", model="m", mode="text"))
        ses.commit()

    summary, reason = maybe_auto_pm_sync(
        "sacrifice", tmp_path, dry_run=True, state_db_path=db
    )
    assert summary is None and reason == "rate_limited"


def test_github_client_factory_not_called_when_nothing_pending(tmp_path: Path) -> None:
    """Ticks on hosts without GitHub creds must not fail on an idle queue."""
    db = _seed_app(tmp_path)
    _write_settings(tmp_path, enabled=True)

    def _boom() -> None:
        raise AssertionError("factory must not be called with no pending work")

    summary, reason = maybe_auto_pm_sync(
        "sacrifice", tmp_path, dry_run=False, github_client_factory=_boom, state_db_path=db
    )
    assert summary is None and reason == "no_pending"
