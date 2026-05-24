"""Phase-6 scheduled persona runner + chain integration.

Public entry point: ``run_scheduled_persona(persona, app,
software_factory_root, *, dry_run=False) -> ScheduledRunRecord``.

What the function does:

  1. Loads the persona prompt from ``factory/personas/<persona>.md``.
  2. Composes a context prelude (current-state + module map).
  3. Invokes ``runner.text_run`` (ralph/bug_hunter/security) or
     ``runner.sandbox_run`` (ux_auditor — needs the browser tool).
  4. Parses the structured JSON output.
  5. For each finding/drift, calls
     ``factory.directions.creator.create_direction`` to file a fresh
     direction directory under ``apps/<app>/directions/``.
  6. Persists a ``ScheduledRunRecord`` row.

Dry-run is truly dry: no LLM call, no GitHub call, no real subprocesses.
The fixture path returns a deterministic ``ScheduledRunOutput`` per
persona so the CLI's ``--dry-run`` flag can exercise the full chain
end-to-end.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Field, Session, SQLModel, create_engine

from factory.directions.creator import create_direction
from factory.directions.parser import Direction
from factory.model_router import route

# --------------------------------------------------------------------------- #
# DB
# --------------------------------------------------------------------------- #


class ScheduledRunRecord(SQLModel, table=True):
    """Per-run audit row for the scheduled personas.

    Recorded for every dispatch — success, failure, or skip. The cron
    scheduler reads these to enforce rate limits.
    """

    __tablename__ = "scheduled_runs"

    id: int | None = Field(default=None, primary_key=True)
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(), index=True)
    persona: str = Field(index=True)
    app: str = Field(index=True)
    duration_s: float = 0.0
    findings_count: int = 0
    directions_filed_json: str = "[]"  # JSON list of direction ids
    status: str = "ok"  # 'ok' | 'errored' | 'dry_run'
    error: str | None = None
    dry_run: bool = False


def _engine(db_path: Path) -> Any:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(f"sqlite:///{db_path}", echo=False)
    SQLModel.metadata.create_all(eng)
    return eng


# --------------------------------------------------------------------------- #
# Output dataclasses
# --------------------------------------------------------------------------- #


@dataclass
class ScheduledRunOutput:
    """Aggregated result returned to the CLI."""

    persona: str
    app: str
    findings_count: int = 0
    directions_filed: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    status: str = "ok"
    error: str | None = None
    dry_run: bool = False
    raw_output: dict[str, Any] = field(default_factory=dict)


# Personas Phase 6 knows about and the JSON key under which they emit
# findings/drifts. Drives both the dry-run fixture and the live parser.
_PERSONA_FINDINGS_KEY: dict[str, str] = {
    "ralph": "drifts",
    "bug_hunter": "findings",
    "security": "findings",
    "ux_auditor": "findings",
}

# Strict cap on output tokens — Ralph runs hourly and must stay cheap.
_OUTPUT_TOKEN_CAP: dict[str, int] = {
    "ralph": 1024,
    "bug_hunter": 2048,
    "security": 3000,
    "ux_auditor": 3000,
}


# --------------------------------------------------------------------------- #
# Dry-run fixtures
# --------------------------------------------------------------------------- #


def _dry_run_fixture(persona: str, app: str) -> dict[str, Any]:
    """Deterministic per-persona fixture used by ``--dry-run`` paths.

    Each fixture mirrors the persona's documented output schema so the
    direction-creator and DB-record code paths exercise the same shapes
    they would on a real run.
    """
    if persona == "ralph":
        return {
            "drifts": [
                {
                    "kind": "spec",
                    "target": "backend/api/healthz.py",
                    "description": (
                        f"Fixture: PRD says GET /healthz returns "
                        f"{{version, status}} but the test "
                        f"test_healthz_returns_version is failing for {app}."
                    ),
                    "suggested_direction": {
                        "title": "fix /healthz returns version+status",
                        "type": "bug",
                        "why": (
                            f"Ralph dry-run fixture: PRD-mandated behavior is broken on {app}."
                        ),
                        "acceptance": [
                            "GET /healthz returns 200 with {version: str, status: 'ok'}",
                        ],
                    },
                }
            ],
            "runs_completed": ["test_command:dry_run"],
            "duration_s": 0.01,
        }
    if persona == "bug_hunter":
        return {
            "findings": [
                {
                    "tool": "semgrep",
                    "rule_id": "python.lang.security.audit.subprocess-shell-true",
                    "severity": "high",
                    "files": ["backend/services/exec.py:12"],
                    "summary": (
                        "Fixture: subprocess invoked with shell=True on user-controlled input."
                    ),
                    "suggested_direction": {
                        "title": "fix subprocess shell=True in exec.py",
                        "type": "security",
                        "why": ("Bug-hunter dry-run fixture: shell injection risk."),
                        "acceptance": [
                            "exec.py invocations no longer use shell=True on user input",
                        ],
                    },
                }
            ],
            "runs_completed": ["semgrep:dry_run"],
            "duration_s": 0.01,
        }
    if persona == "security":
        return {
            "threat_model_summary": (
                "Fixture: app posture appears defense-in-depth at the "
                "auth layer but missing rate limits on /api/pledge."
            ),
            "findings": [
                {
                    "asset": "pledge integrity",
                    "actor": "authenticated user",
                    "path": (
                        "Endpoint /api/pledge has no rate limit; "
                        "an authenticated user can flood pledges, "
                        "corrupting goal totals."
                    ),
                    "severity": "medium",
                    "evidence": ["backend/api/pledge.py:30"],
                    "mitigation": "add per-user rate limit on /api/pledge",
                    "suggested_direction": {
                        "title": "rate-limit /api/pledge",
                        "type": "security",
                        "why": (
                            "Security dry-run fixture: pledge flooding can corrupt goal totals."
                        ),
                        "acceptance": [
                            "POST /api/pledge returns 429 after >5 pledges/min per user",
                        ],
                    },
                }
            ],
            "runs_completed": ["threat_model:dry_run"],
            "duration_s": 0.01,
        }
    if persona == "ux_auditor":
        return {
            "findings": [
                {
                    "flow": "pledge-flow.md",
                    "step": 4,
                    "kind": "friction",
                    "evidence": (
                        "Fixture: getByRole('button', name='Confirm') "
                        "requires 6 clicks to confirm a single pledge."
                    ),
                    "suggestion": "collapse the confirmation sub-flow to 2 clicks",
                    "suggested_direction": {
                        "title": "collapse pledge confirmation to 2 clicks",
                        "type": "ux",
                        "why": (
                            "UX-auditor dry-run fixture: confirmation flow "
                            "is 6 clicks for a 2-click task."
                        ),
                        "acceptance": [
                            "User can confirm a pledge in <= 2 clicks from the goal page",
                        ],
                    },
                }
            ],
            "duration_s": 0.01,
        }
    return {"findings": [], "duration_s": 0.0}


# --------------------------------------------------------------------------- #
# Direction filing
# --------------------------------------------------------------------------- #


def _slugify_title(title: str) -> str:
    import re

    s = re.sub(r"[^A-Za-z0-9]+", "-", title.strip().lower()).strip("-")
    return (s[:40] or "ralph-finding").strip("-") or "ralph-finding"


def _file_finding_as_direction(
    *,
    persona: str,
    app: str,
    finding: dict[str, Any],
    software_factory_root: Path,
) -> Direction | None:
    """Create a direction directory for one finding. Returns the parsed Direction.

    Falls back gracefully if the finding lacks a ``suggested_direction``
    block (returns ``None``).
    """
    suggested = finding.get("suggested_direction")
    if not isinstance(suggested, dict):
        return None
    title = str(suggested.get("title") or "").strip()
    type_tag = str(suggested.get("type") or "bug").strip()
    why = str(suggested.get("why") or "").strip()
    acceptance_raw = suggested.get("acceptance") or []
    acceptance = [str(a) for a in acceptance_raw if str(a).strip()]
    if not title or not why:
        return None
    created = create_direction(
        app,
        title=title,
        type_tag=type_tag,
        why=why,
        has_ui=False,
        flow_steps=None,
        has_api=False,
        api_spec_lines=None,
        acceptance=acceptance,
        explore=False,
        attach_files=None,
        software_factory_root=software_factory_root,
        source=f"scheduled-{persona}",
    )
    return created.direction


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #


def run_scheduled_persona(
    persona: str,
    app: str,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    fixture_output: dict[str, Any] | None = None,
) -> ScheduledRunOutput:
    """Execute one scheduled persona run.

    On ``dry_run=True`` no LLM is called and no GH issue is opened, but
    directions ARE filed on disk (so the CLI's --dry-run is end-to-end
    testable without API keys). Pass ``fixture_output`` to override the
    default per-persona fixture.

    On real-run, ``text_run`` is invoked with the persona prompt + the
    composed context prelude; the output is parsed as JSON and findings
    are filed.

    Rate-limit gate runs FIRST: ``can_dispatch(persona, app, state,
    settings)`` consults the per-persona daily-run cap recorded in
    ``factory_settings.yaml`` (``rate_limits.<persona>_runs_per_day``).
    When refused, the run records a ``rejected`` row with the canonical
    ``rejected_reason`` and returns immediately (no LLM, no directions).
    """
    root = Path(software_factory_root)
    db = db_path or (root / "state" / "factory.db")
    started = datetime.now(UTC)
    findings_key = _PERSONA_FINDINGS_KEY.get(persona)
    if findings_key is None:
        return _record_and_return(
            persona=persona,
            app=app,
            duration_s=0.0,
            findings_count=0,
            directions_filed=[],
            status="errored",
            error=f"unknown_scheduled_persona: {persona!r}",
            dry_run=dry_run,
            raw_output={},
            db_path=db,
        )

    # Rate-limit gate. Phase 6 personas (ralph/bug_hunter/security/
    # ux_auditor) each have a per-day cap in
    # ``factory_settings.yaml.rate_limits``; ``can_dispatch`` returns
    # ``rejected_reason="<persona>_rate_limit_exceeded"`` when the cap is
    # hit. Runs that are refused are recorded with status="rejected" so
    # the audit trail captures the attempt.
    from factory.settings.enforcer import can_dispatch
    from factory.settings.loader import load_settings
    from factory.settings.modes import get_mode
    from factory.settings.spend import (
        hour_spend_usd,
        persona_runs_today,
        today_spend_usd,
    )

    if persona in _PERSONA_FINDINGS_KEY:
        settings = load_settings(root)
        state = {
            "mode": get_mode(root, db_path=db),
            "global_in_flight": 0,
            "app_in_flight": 0,
            "today_spend_usd": today_spend_usd(root, db_path=db),
            "hour_spend_usd": hour_spend_usd(root, db_path=db),
            "open_prs_for_app": None,
            "failing_ci_count": None,
            "pm_invocations_last_hour": 0,
            f"{persona}_runs_today": persona_runs_today(persona, root, db_path=db),
        }
        decision = can_dispatch(persona, app, state, settings)
        if not decision.allowed:
            duration = (datetime.now(UTC) - started).total_seconds()
            return _record_and_return(
                persona=persona,
                app=app,
                duration_s=duration,
                findings_count=0,
                directions_filed=[],
                status="rejected",
                error=decision.rejected_reason,
                dry_run=dry_run,
                raw_output={},
                db_path=db,
            )

    raw: dict[str, Any]
    error: str | None = None
    if dry_run:
        raw = fixture_output if fixture_output is not None else _dry_run_fixture(persona, app)
    else:
        try:
            raw = _live_run(persona, app, root)
        except Exception as exc:  # noqa: BLE001 - capture all exceptions for audit
            duration = (datetime.now(UTC) - started).total_seconds()
            return _record_and_return(
                persona=persona,
                app=app,
                duration_s=duration,
                findings_count=0,
                directions_filed=[],
                status="errored",
                error=str(exc),
                dry_run=False,
                raw_output={},
                db_path=db,
            )

    findings = raw.get(findings_key) or []
    if not isinstance(findings, list):
        findings = []

    directions_filed: list[str] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        try:
            direction = _file_finding_as_direction(
                persona=persona,
                app=app,
                finding=finding,
                software_factory_root=root,
            )
        except Exception as exc:  # noqa: BLE001 - one bad finding shouldn't kill the run
            error = f"direction_create_failed: {exc}"
            continue
        if direction is not None:
            directions_filed.append(direction.id)

    duration = (datetime.now(UTC) - started).total_seconds()
    return _record_and_return(
        persona=persona,
        app=app,
        duration_s=duration,
        findings_count=len(findings),
        directions_filed=directions_filed,
        status="dry_run" if dry_run else "ok",
        error=error,
        dry_run=dry_run,
        raw_output=raw,
        db_path=db,
    )


def _record_and_return(
    *,
    persona: str,
    app: str,
    duration_s: float,
    findings_count: int,
    directions_filed: list[str],
    status: str,
    error: str | None,
    dry_run: bool,
    raw_output: dict[str, Any],
    db_path: Path,
) -> ScheduledRunOutput:
    eng = _engine(db_path)
    rec = ScheduledRunRecord(
        persona=persona,
        app=app,
        duration_s=round(duration_s, 4),
        findings_count=findings_count,
        directions_filed_json=json.dumps(directions_filed),
        status=status,
        error=error,
        dry_run=dry_run,
    )
    with Session(eng) as session:
        session.add(rec)
        session.commit()
        session.refresh(rec)
    # Update the schedule's last-run timestamp so the cron scheduler
    # doesn't re-fire this slot. Best-effort; failure here is recorded
    # but doesn't fail the run.
    try:
        from factory.scheduler.cron import load_schedules, upsert_schedule_row

        for sched in load_schedules(Path(db_path).parent.parent):
            if sched.persona == persona:
                upsert_schedule_row(
                    name=sched.name,
                    cron_expr=sched.cron_expr,
                    last_run=datetime.now(UTC).isoformat(),
                    last_status=status,
                    db_path=db_path,
                )
    except Exception:  # noqa: BLE001 - schedule update is best-effort
        pass
    return ScheduledRunOutput(
        persona=persona,
        app=app,
        findings_count=findings_count,
        directions_filed=directions_filed,
        duration_s=round(duration_s, 4),
        status=status,
        error=error,
        dry_run=dry_run,
        raw_output=raw_output,
    )


def _live_run(persona: str, app: str, software_factory_root: Path) -> dict[str, Any]:
    """Compose context + persona prompt + dispatch via runner.

    Ralph/bug_hunter/security/ux_auditor all use ``text_run`` for v1; the
    sandbox path (browser tool) is reserved for a future ux_auditor
    enhancement when the live deploy URL exists.
    """
    from factory.app_config import load_app_config
    from factory.context.loader import compose_context_prelude
    from factory.runner import text_run

    _ = load_app_config(app, software_factory_root)  # ensure config exists
    prelude = compose_context_prelude(
        persona,
        app_repo_path=software_factory_root / "apps" / app,
        task_scope=None,
    )
    persona_md_path = Path(__file__).resolve().parent.parent / "personas" / f"{persona}.md"
    persona_prompt = persona_md_path.read_text(encoding="utf-8") if persona_md_path.exists() else ""
    prompt = f"{persona_prompt}\n\n# Context prelude\n\n{prelude}\n"
    model = route(persona)
    max_tokens = _OUTPUT_TOKEN_CAP.get(persona, 2048)
    result = text_run(
        persona,
        prompt,
        model,
        schema={"type": "object"},
        max_tokens=max_tokens,
    )
    if isinstance(result, dict):
        return result
    # text_run returned a raw string (schema not respected); try to parse.
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}
