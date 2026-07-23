"""Factory self-improvement entry point.

Reads recent ``factory_needs_redesign`` events from
``state/logs/*.log``, terminally-blocked stories from
``state/factory.db``, and the current persona/state-machine layout;
invokes the ``factory_improver`` persona via ``text_run``; persists the
proposal to ``state/improvements/<timestamp>.json`` AND updates a
pinned GitHub issue (``factory-improvements`` label) so the operator
sees a single rolling thread, not a spam fountain.

Public entry points
===================

* ``aggregate_factory_needs_redesign_events`` — pure helper, scans
  ``state/logs/`` for JSONL records and returns the recent window.
* ``run_factory_improver`` — full pipeline, called by both the CLI
  (``factory improve``) and the scheduled-personas hook.

Idempotency
===========

The GitHub issue update path looks up an existing open issue with the
``factory-improvements`` label first. When one exists, the run posts a
comment on it (not a new issue). When none exists, the run opens one.
This keeps the operator's notification surface to a single issue thread
regardless of how often the improver runs.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, create_engine, select

from factory.chain.state_machine import _TRANSITIONS, StoryRecord, StoryState

# Upper bound on the assembled JSON bundle handed to the persona.
# The personas/ tree alone is ~90KB; events + state machine round it
# up. 250KB stays well inside cheap-model context budgets and gives
# the persona room to write *applicable* unified diffs (the previous
# 80KB cap was set when only the personas_index — names only — was
# in the bundle).
_PROMPT_BUNDLE_CHAR_LIMIT = 250_000


# Schema the persona's JSON output is validated against. We use the
# shape directly in text_run so litellm requests JSON mode.
_IMPROVER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["improvements", "summary", "events_processed"],
    "properties": {
        "improvements": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind", "target", "rationale"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "prompt_edit",
                            "doc_update",
                            "new_state",
                            "new_handler",
                            "workflow_change",
                        ],
                    },
                    "target": {"type": "string"},
                    "rationale": {"type": "string"},
                    "suggested_patch": {"type": "string"},
                    "evidence": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                },
            },
        },
        "summary": {"type": "string"},
        "events_processed": {"type": "integer"},
    },
}


@dataclass
class FactoryImproverResult:
    """Returned to the CLI / scheduled-run wrapper."""

    timestamp: str
    output_path: Path | None = None
    issue_number: int | None = None
    events_processed: int = 0
    improvements_count: int = 0
    raw_output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    dry_run: bool = False
    apply_summary: Any = None  # ApplyPassSummary, when L2 apply pass ran

    @property
    def succeeded(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# Event aggregation
# ---------------------------------------------------------------------------


def aggregate_factory_needs_redesign_events(
    *,
    software_factory_root: Path,
    window_hours: int = 24,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Walk ``state/logs/*.log`` and return all ``factory_needs_redesign``
    events whose ``ts`` falls within the last ``window_hours``.

    Pure helper — no LLM, no GH. The events are returned oldest-first
    so the persona prompt's "(most recent last)" ordering holds.

    A malformed log line or a record missing ``ts`` is skipped silently;
    the function is best-effort by design (it MUST tolerate the messy
    state of a long-running deployment).
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=window_hours)
    logs_dir = Path(software_factory_root) / "state" / "logs"
    if not logs_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for log_path in sorted(logs_dir.glob("*.log")):
        try:
            with log_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Valid JSON that isn't an object (a bare int/str from a
                    # stray non-NDJSON file in state/logs/) must be skipped,
                    # not crash the tick.
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("event") != "factory_needs_redesign":
                        continue
                    ts = rec.get("ts")
                    if not isinstance(ts, str):
                        continue
                    try:
                        ts_dt = datetime.fromisoformat(ts)
                    except ValueError:
                        continue
                    if ts_dt.tzinfo is None:
                        ts_dt = ts_dt.replace(tzinfo=UTC)
                    if ts_dt < cutoff:
                        continue
                    rec["_log_file"] = log_path.name
                    out.append(rec)
        except OSError:
            continue
    out.sort(key=lambda r: r.get("ts", ""))
    return out


def _terminally_blocked_stories(
    *,
    db_path: Path,
    app: str | None,
    window_hours: int = 24,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return story rows in terminal blocked states, optionally filtered by app."""
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=window_hours)
    if not db_path.exists():
        return []
    eng = create_engine(f"sqlite:///{db_path}", echo=False)
    blocked_states = (
        StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value,
        StoryState.BLOCKED_DEPLOY_FAILED.value,
        StoryState.BLOCKED_REVIEW_NONCONVERGENT.value,
        StoryState.BLOCKED_CI_UNRESOLVED.value,
    )
    with Session(eng) as session:
        query = select(StoryRecord).where(
            StoryRecord.state.in_(blocked_states)  # type: ignore[attr-defined]
        )
        if app is not None:
            query = query.where(StoryRecord.app == app)
        rows = session.exec(query).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            updated = datetime.fromisoformat(row.updated_at)
        except (ValueError, TypeError):
            updated = None
        if updated is not None:
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if updated < cutoff:
                continue
        out.append(
            {
                "id": row.id,
                "app": row.app,
                "slug": row.slug,
                "scope": row.scope,
                "state": row.state,
                "direction_id": row.direction_id,
                "dev_retries": row.dev_retries,
                "error": row.error,
                "updated_at": row.updated_at,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Event-trigger gate
# ---------------------------------------------------------------------------
#
# The improver used to fire on a cron (daily at 05:00 UTC, twice per day
# cap). That made the feedback loop ~12h: an event written at 06:00
# would sit until ~17:00 before the persona even looked at it. The
# trigger is now event-driven — ``factory tick`` calls
# ``should_fire_improver`` every tick and fires when a fresh
# ``factory_needs_redesign`` event exists AND debounce has elapsed AND
# the daily cap isn't reached.
#
# Run-history is persisted as a JSONL-ish list at
# ``state/.improver_run_history.json``. Pruned to a 24h window on every
# write so it stays tiny.

_HISTORY_FILENAME = ".improver_run_history.json"


def _history_path(software_factory_root: Path) -> Path:
    return Path(software_factory_root) / "state" / _HISTORY_FILENAME


def _load_history(software_factory_root: Path) -> list[str]:
    """Return the list of run-start ISO timestamps, oldest-first. Best-effort."""
    p = _history_path(software_factory_root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [t for t in data if isinstance(t, str)] if isinstance(data, list) else []


def _parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def should_fire_improver(
    *,
    software_factory_root: Path,
    debounce_minutes: int = 10,
    daily_cap: int = 12,
    window_hours: int = 24,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Decide whether ``factory tick`` should fire the improver now.

    The gate is a conjunction of:

    * **Event-newer-than-last-run** — at least one
      ``factory_needs_redesign`` event in the window has a ``ts`` after
      our most recent run. The first ever run fires as long as there
      is *any* event in the window.
    * **Debounce** — ``debounce_minutes`` since the last run, so a
      burst of events doesn't kick off a stampede of LLM calls.
    * **Daily cap** — at most ``daily_cap`` runs in the trailing 24h
      window. Circuit-breaker against a runaway persona / event flood.

    Returns ``(fire, reason)``. ``reason`` is a short human-readable
    string the ``tick`` command surfaces in its results table so the
    operator can see why a run was or wasn't dispatched.

    Pure with respect to clock + filesystem state; ``now`` is
    injectable for tests.
    """
    now = now or datetime.now(UTC)
    history = _load_history(software_factory_root)

    # Daily cap. We trim the history view to the trailing 24h so a
    # cap of N means "N in the last day", not "N since the dawn of
    # time."
    day_cutoff = now - timedelta(hours=24)
    recent_runs = [
        t for t in history if (_parse_ts(t) or day_cutoff - timedelta(days=999)) >= day_cutoff
    ]
    if len(recent_runs) >= daily_cap:
        return False, f"daily_cap_reached:{len(recent_runs)}/{daily_cap}"

    # Debounce.
    if recent_runs:
        last_dt = _parse_ts(recent_runs[-1])
        if last_dt is not None:
            elapsed = now - last_dt
            if elapsed < timedelta(minutes=debounce_minutes):
                secs = int(elapsed.total_seconds())
                return False, f"debounce:{secs}s<{debounce_minutes}m"

    events = aggregate_factory_needs_redesign_events(
        software_factory_root=software_factory_root,
        window_hours=window_hours,
        now=now,
    )
    if not events:
        return False, "no_events_in_window"

    newest_event_ts = max(
        (_parse_ts(e.get("ts", "")) for e in events if isinstance(e.get("ts"), str)),
        default=None,
    )
    if newest_event_ts is None:
        return False, "no_parseable_event_ts"

    if recent_runs:
        last_dt = _parse_ts(recent_runs[-1])
        if last_dt is not None and newest_event_ts <= last_dt:
            return False, "no_new_events_since_last_run"

    return True, f"fire:events={len(events)}"


def record_improver_fired(
    software_factory_root: Path, *, now: datetime | None = None
) -> None:
    """Append ``now`` to the history file, then prune to the last 24h.

    Called by the tick path right after a successful (or attempted —
    we count attempts toward the cap) ``run_factory_improver`` invocation.
    """
    now = now or datetime.now(UTC)
    p = _history_path(software_factory_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    history = _load_history(software_factory_root)
    history.append(now.isoformat())
    cutoff = now - timedelta(hours=24)
    history = [
        t
        for t in history
        if (_parse_ts(t) or cutoff - timedelta(days=999)) >= cutoff
    ]
    p.write_text(json.dumps(history), encoding="utf-8")


_APPLY_PASS_LOG = "_factory_improver_apply.log"


def _make_apply_log_event(software_factory_root: Path) -> Any:
    """Return a ``log_event(kind, payload)`` callable that appends a
    JSONL record to ``state/logs/_factory_improver_apply.log``.

    Plain-text per-line JSON so ``factory why`` / ``grep`` work on it
    just like any other event log. Pre-pended ``_`` keeps it out of the
    per-story-log glob.
    """
    logs_dir = software_factory_root / "state" / "logs"
    log_path = logs_dir / _APPLY_PASS_LOG

    def _log(kind: str, payload: dict[str, Any]) -> None:
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "event": kind,
                            **payload,
                        }
                    )
                    + "\n"
                )
        except OSError:
            # Best-effort — never fail the apply pass over a log
            # write failure.
            pass

    return _log


def _personas_index(personas_dir: Path) -> list[dict[str, Any]]:
    """Compose a lightweight index of every persona prompt.

    Bytes + sha256-prefix lets the persona reason about which prompts
    have been edited recently without us having to embed the full
    prompt text in its input (would be hundreds of KB).
    """
    out: list[dict[str, Any]] = []
    for p in sorted(personas_dir.glob("*.md")):
        try:
            data = p.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(data).hexdigest()[:12]
        out.append(
            {
                "name": p.stem,
                "bytes": len(data),
                "sha256_prefix": digest,
            }
        )
    return out


def _personas_full_text(personas_dir: Path) -> dict[str, str]:
    """Return ``{persona_name: full_markdown_text}`` for every persona.

    Required for the improver to write *applicable* unified diffs —
    without the actual line contents it can only hallucinate context.
    Total cost is ~90KB; the prompt assembler truncates the bundle at
    ``_PROMPT_BUNDLE_CHAR_LIMIT`` so a runaway personas/ doesn't blow
    the context window.
    """
    out: dict[str, str] = {}
    for p in sorted(personas_dir.glob("*.md")):
        try:
            out[p.stem] = p.read_text(encoding="utf-8")
        except OSError:
            continue
    return out


def _state_machine_summary() -> list[dict[str, str]]:
    """Return ``_TRANSITIONS`` as a JSON-serialisable list of triples."""
    out: list[dict[str, str]] = []
    for (state, event), next_state in _TRANSITIONS.items():
        out.append(
            {
                "state": state.value,
                "event": event,
                "next_state": next_state.value,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Pinned-issue idempotent post
# ---------------------------------------------------------------------------


_PINNED_ISSUE_LABEL = "factory-improvements"
_PINNED_ISSUE_TITLE = "Factory improvements — rolling proposals from `factory improve`"


def post_to_pinned_issue(
    *,
    repo: str,
    body: str,
    gh_runner: Any = None,
) -> tuple[int | None, str | None]:
    """Idempotently update the pinned ``factory-improvements`` issue.

    Looks for an open issue labeled ``factory-improvements``. If one
    exists, posts ``body`` as a comment. If none exists, opens a new
    issue with that label.

    Returns ``(issue_number, error)``. ``error`` is None on success.

    ``gh_runner`` is injected for tests — pass a callable with the
    same signature as ``subprocess.run`` that records the args. The
    default is ``subprocess.run`` for production.
    """
    runner: Any = gh_runner or subprocess.run

    # 1. Find an existing open issue with the label.
    list_proc = runner(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            _PINNED_ISSUE_LABEL,
            "--state",
            "open",
            "--json",
            "number,title",
            "--limit",
            "5",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if list_proc.returncode != 0:
        return None, f"gh_list_failed: {(list_proc.stderr or '').strip()[:200]}"

    existing_number: int | None = None
    try:
        items = json.loads(list_proc.stdout or "[]")
        if items and isinstance(items, list):
            existing_number = int(items[0].get("number"))
    except (json.JSONDecodeError, TypeError, ValueError):
        existing_number = None

    if existing_number is not None:
        # 2a. Comment on the existing issue.
        comment_proc = runner(
            [
                "gh",
                "issue",
                "comment",
                str(existing_number),
                "--repo",
                repo,
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if comment_proc.returncode != 0:
            return existing_number, (
                f"gh_comment_failed: {(comment_proc.stderr or '').strip()[:200]}"
            )
        return existing_number, None

    # 2b. No existing issue — open a new one with the label.
    create_proc = runner(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            _PINNED_ISSUE_TITLE,
            "--body",
            body,
            "--label",
            _PINNED_ISSUE_LABEL,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if create_proc.returncode != 0:
        return None, f"gh_create_failed: {(create_proc.stderr or '').strip()[:200]}"
    import re

    m = re.search(r"/issues/(\d+)", create_proc.stdout or "")
    return (int(m.group(1)) if m else None), None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_factory_improver(
    *,
    app: str | None,
    software_factory_root: Path,
    window_hours: int = 24,
    db_path: Path | None = None,
    dry_run: bool = False,
    fixture_output: dict[str, Any] | None = None,
    gh_runner: Any = None,
    repo_for_issue: str | None = None,
    apply_pass: bool = True,
    apply_repo: str | None = None,
    apply_runner: Any = None,
) -> FactoryImproverResult:
    """Full pipeline. Returns ``FactoryImproverResult``.

    Steps:
      1. Aggregate events + blocked stories + personas + transitions.
      2. Compose the input bundle for the persona.
      3. Invoke ``text_run("factory_improver", ...)`` (dry-run uses
         ``fixture_output`` for deterministic tests).
      4. Write the JSON output to
         ``state/improvements/<timestamp>.json``.
      5. Post a summary on the pinned GH issue (skipped on dry-run).
    """
    root = Path(software_factory_root)
    db = db_path or (root / "state" / "factory.db")
    ts = datetime.now(UTC).isoformat()

    events = aggregate_factory_needs_redesign_events(
        software_factory_root=root, window_hours=window_hours
    )
    blocked = _terminally_blocked_stories(db_path=db, app=app, window_hours=window_hours)
    personas_dir = Path(__file__).resolve().parent.parent / "personas"
    personas = _personas_index(personas_dir)
    personas_text = _personas_full_text(personas_dir)
    transitions = _state_machine_summary()

    bundle = {
        "events_window": events,
        "blocked_stories": blocked,
        "personas_index": personas,
        # Full markdown for every persona — required so the improver
        # can emit unified diffs whose context lines match the real
        # file contents. Without these, ``git apply`` would reject
        # every patch and the L2 apply pass would drop every proposal
        # as ``invalid``.
        "personas_full_text": personas_text,
        "state_machine_summary": transitions,
        "window_hours": window_hours,
        "app_filter": app,
    }

    if dry_run:
        raw = fixture_output if fixture_output is not None else _dry_run_fixture(events, blocked)
    else:
        from factory.model_router import max_output_tokens_for, route
        from factory.runner import _read_persona_prompt, text_run

        persona_prompt = _read_persona_prompt("factory_improver")
        prompt = (
            f"{persona_prompt.rstrip()}\n\n"
            "---\n\n"
            "## Input bundle\n\n"
            "```json\n"
            # 250KB cap: the personas/ tree is ~90KB; the rest is
            # events + state machine. Cheap models handle this fine.
            f"{json.dumps(bundle, indent=2)[:_PROMPT_BUNDLE_CHAR_LIMIT]}\n"
            "```\n\n"
            "Return ONLY the JSON object. No prose outside the JSON."
        )
        model_id = route("factory_improver")
        try:
            result = text_run(
                persona="factory_improver",
                prompt=prompt,
                model_id=model_id,
                schema=_IMPROVER_SCHEMA,
                max_tokens=max_output_tokens_for(model_id),
            )
        except Exception as exc:  # noqa: BLE001 - capture for the caller
            return FactoryImproverResult(
                timestamp=ts,
                events_processed=len(events),
                error=f"text_run_failed: {exc!r}",
                dry_run=False,
            )
        if not isinstance(result, dict):
            return FactoryImproverResult(
                timestamp=ts,
                events_processed=len(events),
                error="non_dict_persona_output",
                dry_run=False,
            )
        raw = result

    # Persist the proposal.
    out_dir = root / "state" / "improvements"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = ts.replace(":", "").replace("+", "_")
    out_path = out_dir / f"{safe_ts}.json"
    out_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    improvements_count = len(raw.get("improvements") or [])

    # L2 apply pass — classify, apply, open PRs. Disabled on dry-run
    # (no branches/PRs created in tests by default), and disabled
    # entirely via ``apply_pass=False`` for the ``factory improve
    # --no-apply`` CLI path.
    apply_summary = None
    if apply_pass and not dry_run and improvements_count > 0:
        from factory.chain.factory_improver_apply import run_apply_pass

        apply_summary = run_apply_pass(
            out_path,
            root,
            repo=apply_repo,
            runner=apply_runner,
            open_prs=apply_repo is not None,
            log_event=_make_apply_log_event(root),
        )

    # Post on the pinned issue (real-run only). Skipped in dry-run so
    # tests don't shell out to ``gh``. Body now embeds the apply-pass
    # summary when one ran.
    issue_number: int | None = None
    if not dry_run and repo_for_issue:
        body = _format_issue_body(
            raw,
            events_count=len(events),
            ts=ts,
            apply_summary=apply_summary,
        )
        issue_number, err = post_to_pinned_issue(
            repo=repo_for_issue, body=body, gh_runner=gh_runner
        )
        if err:
            # Don't fail the run — the JSON is already on disk; the
            # issue update is the surface, not the durable record.
            raw["_post_warning"] = err

    return FactoryImproverResult(
        timestamp=ts,
        output_path=out_path,
        issue_number=issue_number,
        events_processed=len(events),
        improvements_count=improvements_count,
        raw_output=raw,
        dry_run=dry_run,
        apply_summary=apply_summary,
    )


def _format_issue_body(
    raw: dict[str, Any],
    *,
    events_count: int,
    ts: str,
    apply_summary: Any = None,
) -> str:
    """Markdown body for the pinned-issue comment / new-issue body.

    When ``apply_summary`` is supplied (L2 apply pass ran), its counts
    table is embedded after the per-proposal list so the operator can
    see at a glance which proposals turned into PRs.
    """
    lines = [
        f"### Factory-improver run @ {ts}",
        "",
        f"- Events processed: **{events_count}**",
        f"- Improvements proposed: **{len(raw.get('improvements') or [])}**",
        "",
        "**Summary**",
        "",
        (raw.get("summary") or "_(no summary)_").strip(),
        "",
        "**Proposed improvements**",
        "",
    ]
    if not raw.get("improvements"):
        lines.append("_(none — factory appears healthy in this window)_")
    else:
        for i, imp in enumerate(raw.get("improvements") or [], 1):
            lines.append(
                f"{i}. **{imp.get('kind', '?')}** → `{imp.get('target', '?')}`  "
                f"_(confidence: {imp.get('confidence', '?')})_"
            )
            rationale = imp.get("rationale") or ""
            if rationale:
                lines.append(f"   - Why: {rationale}")
            evidence = imp.get("evidence") or ""
            if evidence:
                lines.append(f"   - Evidence: `{evidence}`")
            patch = (imp.get("suggested_patch") or "").strip()
            if patch:
                lines.append("   - Suggested patch:")
                lines.append("     ```")
                for ln in patch.splitlines():
                    lines.append("     " + ln)
                lines.append("     ```")
    if apply_summary is not None:
        from factory.chain.factory_improver_apply import format_apply_pass_md

        lines.append("")
        lines.append(format_apply_pass_md(apply_summary))
    lines.append("")
    lines.append(
        f"_Persisted at_ `state/improvements/{ts.replace(':', '').replace('+', '_')}.json`"
    )
    return "\n".join(lines)


def _dry_run_fixture(
    events: list[dict[str, Any]], blocked: list[dict[str, Any]]
) -> dict[str, Any]:
    """Deterministic fixture used in dry-run mode.

    Mirrors a realistic improver output so the CLI's ``--dry-run`` path
    exercises persistence + formatting without an LLM call.
    """
    improvements: list[dict[str, Any]] = []
    if events:
        improvements.append(
            {
                "kind": "workflow_change",
                "target": "factory/chain/orchestrator.py",
                "rationale": (
                    f"Saw {len(events)} factory_needs_redesign event(s) in window; "
                    "consider a harness_precheck step before dev."
                ),
                "suggested_patch": (
                    "Add HARNESS_PRECHECK_IN_PROGRESS state and dispatch a "
                    "test-collect-only pass before transitioning to "
                    "DEV_IN_PROGRESS."
                ),
                "evidence": f"events[0]._log_file={events[0].get('_log_file')}",
                "confidence": "medium",
            }
        )
    return {
        "improvements": improvements,
        "summary": (
            f"Dry-run fixture: {len(events)} event(s), {len(blocked)} blocked "
            f"story row(s) in the window."
        ),
        "events_processed": len(events),
    }


__all__ = [
    "FactoryImproverResult",
    "aggregate_factory_needs_redesign_events",
    "post_to_pinned_issue",
    "record_improver_fired",
    "run_factory_improver",
    "should_fire_improver",
]
