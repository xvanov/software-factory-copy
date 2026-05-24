"""Ralph chain — hourly spec-vs-reality diff.

``ralph_tick(app, software_factory_root, *, dry_run=False)`` invokes the
Ralph persona, parses its structured JSON output, and (for each finding)
files a new direction via ``factory.directions.creator.create_direction``.

Dry-run is TRULY dry: no LLM call, no scanners, no GitHub. A deterministic
fixture is composed by ``_dry_run_findings`` so the chain shape can be
exercised in CI.

Rate-limit semantics: consult ``can_dispatch("ralph", app, state, settings)``
BEFORE invoking the persona. ``state["ralph_runs_today"]`` is computed by
``persona_runs_today``; the cap is ``rate_limits.ralph_runs_per_day``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.app_config import load_app_config
from factory.directions.creator import create_direction
from factory.directions.parser import Direction
from factory.settings.enforcer import DispatchDecision, can_dispatch
from factory.settings.loader import load_settings
from factory.settings.modes import get_mode
from factory.settings.spend import (
    hour_spend_usd,
    persona_runs_today,
    today_spend_usd,
)


@dataclass
class RalphSummary:
    """Outcome of a single ralph_tick invocation."""

    app: str
    allowed: bool
    rejected_reason: str | None = None
    findings_count: int = 0
    directions_created: list[str] = field(default_factory=list)
    summary: str = ""
    raw_findings: list[dict[str, Any]] = field(default_factory=list)


# Map persona-suggested direction types onto the factory's PM-validated type
# enum. Anything not in the allowlist falls back to "bug" (the chain catches
# that via PM-validation gates downstream).
_TYPE_MAP = {
    "bug": "bug",
    "docs": "docs",
    "refactor": "refactor",
    "spec_drift": "bug",
    "docs_drift": "docs",
    "missing_test": "bug",
}


def _dry_run_findings(app: str) -> dict[str, Any]:
    """Deterministic fixture for dry-run mode.

    Produces two findings — one spec_drift (bug-typed) and one docs_drift
    (docs-typed) — so the dry-run exercises both direction-creation paths.
    """
    return {
        "findings": [
            {
                "kind": "spec_drift",
                "path": "backend/healthz.py",
                "claim": "PRD section 'Health check': /healthz returns {version, status}.",
                "evidence": "Endpoint exists but returns only {status}; no version field.",
                "suggested_direction_type": "bug",
                "suggested_direction_title": "Restore version field in /healthz response",
            },
            {
                "kind": "docs_drift",
                "path": f"apps/{app}/context/modules/payments.md",
                "claim": "modules/payments.md describes legacy_charge() export.",
                "evidence": "legacy_charge() removed in current code; doc still references it.",
                "suggested_direction_type": "docs",
                "suggested_direction_title": "Drop legacy_charge() from modules/payments.md",
            },
        ],
        "summary": (
            "Dry-run: 2 synthetic findings (1 spec_drift, 1 docs_drift) covering "
            "both direction-creation paths."
        ),
    }


def _build_direction_why(finding: dict[str, Any]) -> str:
    return (
        f"Ralph (hourly spec-vs-reality auditor) flagged drift in "
        f"`{finding.get('path', '?')}`:\n\n"
        f"Claim: {finding.get('claim', '(no claim)')}\n"
        f"Evidence: {finding.get('evidence', '(no evidence)')}\n"
    )


def _build_acceptance(finding: dict[str, Any]) -> list[str]:
    kind = finding.get("kind", "")
    if kind == "missing_test":
        return ["Add a test that covers the behavior named in the claim."]
    if kind in {"docs_drift", "docs"}:
        return ["Rewrite the relevant context/modules/*.md to match current code."]
    return ["Restore the behavior named in the claim, with a test that proves it."]


def _create_direction_from_finding(
    app: str,
    finding: dict[str, Any],
    software_factory_root: Path,
) -> str:
    """Translate a Ralph finding into a Direction directory.

    Returns the created direction's id-slug name.
    """
    title = str(finding.get("suggested_direction_title") or "Ralph drift").strip()[:120]
    type_tag = _TYPE_MAP.get(
        str(finding.get("suggested_direction_type") or finding.get("kind") or "bug"),
        "bug",
    )
    created = create_direction(
        app,
        title=title,
        type_tag=type_tag,
        why=_build_direction_why(finding),
        has_ui=False,
        flow_steps=None,
        has_api=False,
        api_spec_lines=None,
        acceptance=_build_acceptance(finding),
        explore=False,
        attach_files=None,
        software_factory_root=software_factory_root,
        source="ralph",
    )
    return created.dir_path.name


def _build_state_for_dispatch(
    software_factory_root: Path,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Compose the state dict consumed by ``can_dispatch``."""
    db = db_path or (Path(software_factory_root) / "state" / "factory.db")
    return {
        "mode": get_mode(software_factory_root, db_path=db),
        "global_in_flight": 0,
        "app_in_flight": 0,
        "today_spend_usd": today_spend_usd(software_factory_root, db_path=db),
        "hour_spend_usd": hour_spend_usd(software_factory_root, db_path=db),
        "open_prs_for_app": None,
        "failing_ci_count": None,
        "pm_invocations_last_hour": 0,
        "ralph_runs_today": persona_runs_today("ralph", software_factory_root, db_path=db),
        "bug_hunter_runs_today": persona_runs_today(
            "bug_hunter", software_factory_root, db_path=db
        ),
        "security_runs_today": persona_runs_today("security", software_factory_root, db_path=db),
        "ux_auditor_runs_today": persona_runs_today(
            "ux_auditor", software_factory_root, db_path=db
        ),
    }


def ralph_tick(
    app: str,
    software_factory_root: Path,
    *,
    dry_run: bool = False,
    fixture_findings: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> RalphSummary:
    """Run one Ralph tick: rate-limit gate → persona → file directions.

    ``fixture_findings`` lets tests inject the exact JSON the persona would
    return (skipping both LLM and the built-in dry-run fixture).
    """
    root = Path(software_factory_root)
    settings = load_settings(root)

    # Ensure the app config exists; missing app is operator error.
    load_app_config(app, root)

    state = _build_state_for_dispatch(root, db_path=db_path)
    decision: DispatchDecision = can_dispatch("ralph", app, state, settings)
    if not decision.allowed:
        return RalphSummary(
            app=app,
            allowed=False,
            rejected_reason=decision.rejected_reason,
            summary=f"refused: {decision.rejected_reason}",
        )

    # Acquire findings.
    if fixture_findings is not None:
        result = fixture_findings
    elif dry_run:
        result = _dry_run_findings(app)
    else:
        # Real-run: invoke the Ralph persona via text_run.
        from factory.context.loader import compose_context_prelude
        from factory.model_router import route
        from factory.runner import _read_persona_prompt, text_run

        persona = "ralph"
        persona_prompt = _read_persona_prompt(persona)
        prelude = compose_context_prelude(
            persona=persona,
            app_repo_path=root / "apps" / app,
            task_scope="ralph",
        )
        full_prompt = (
            f"{persona_prompt.rstrip()}\n\n"
            "---\n\n"
            "## Context\n\n"
            f"{prelude.rstrip()}\n\n"
            "Return the JSON object. No prose outside the JSON."
        )
        model_id = route(persona)
        try:
            raw = text_run(
                persona=persona,
                prompt=full_prompt,
                model_id=model_id,
                schema=None,
                max_tokens=2048,
            )
            if isinstance(raw, str):
                result = json.loads(raw)
            elif isinstance(raw, dict):
                result = raw
            else:
                result = {"findings": [], "summary": "ralph returned non-dict"}
        except (json.JSONDecodeError, TypeError) as exc:
            result = {"findings": [], "summary": f"ralph JSON parse failed: {exc}"}

    findings: list[dict[str, Any]] = list(result.get("findings") or [])
    # Defensive cap so a runaway persona can't flood the queue.
    findings = findings[:20]

    created: list[str] = []
    for f in findings:
        try:
            name = _create_direction_from_finding(app, f, root)
            created.append(name)
        except (FileExistsError, ValueError):
            # Skip duplicates / malformed findings; the next tick can retry.
            continue

    return RalphSummary(
        app=app,
        allowed=True,
        findings_count=len(findings),
        directions_created=created,
        summary=str(result.get("summary") or ""),
        raw_findings=findings,
    )


__all__ = ["RalphSummary", "ralph_tick"]


# Re-export Direction for callers that want to inspect the parser result;
# we don't use it here but it's the canonical record type for created
# directions.
_ = Direction
