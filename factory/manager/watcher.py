"""factory.manager.watcher — L1 Watcher agent (Phase 3).

The watcher is the first LLM-in-the-loop component of the FMS.  It
runs every minute (or on demand via ``factory manager watch``), reads
recent signal streams and detector outputs, assembles a context bundle,
and asks a cheap LLM to summarise what happened and decide whether to
escalate to L2.

Architecture note
-----------------
This module is *plumbing*.  It assembles context, calls the LLM, and
writes the result.  No anomaly judgment lives here — judgment lives in
``factory/personas/manager_watcher.md``.  Detector outputs are passed
to the LLM along with their docstrings so the LLM knows what each result
means (the load-bearing pattern for the FMS).

Public API
----------
* ``run_watcher_once`` — one watcher invocation; returns the full result dict.
* ``run_watcher_daemon`` — loops ``run_watcher_once`` every N seconds.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from factory.manager.detectors import DETECTOR_DOCS, DETECTORS

# ---------------------------------------------------------------------------
# Lazily-imported helpers — imported here at module level so tests can
# monkeypatch them via ``factory.manager.watcher.text_run`` etc.
# ---------------------------------------------------------------------------


def _read_persona_prompt(persona: str) -> str:
    """Thin wrapper around runner._read_persona_prompt for monkeypatching."""
    from factory.runner import _read_persona_prompt as _impl

    return _impl(persona)


def text_run(
    persona: str,
    prompt: str,
    model_id: str,
    schema: dict | None = None,
    **kwargs: Any,
) -> Any:
    """Thin wrapper around runner.text_run for monkeypatching."""
    from factory.runner import text_run as _impl

    return _impl(persona, prompt, model_id, schema=schema, **kwargs)

# Streams the watcher reads raw lines from.
_RAW_STREAMS = ("runs", "ticks", "queue", "webhooks", "git", "spend")

# Cap per stream (recent lines only; older lines are dropped for token budget).
_MAX_LINES_PER_STREAM = 200

# Cap on any single payload string value (chars).
_PAYLOAD_STRING_CAP = 500

# Watcher-notes stream name.
_WATCHER_NOTES_STREAM = "watcher_notes"

# How many prior watcher notes to include for context continuity.
_PRIOR_NOTES_LIMIT = 10

# Schema version emitted by this module.
_SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Helpers — stream reading
# --------------------------------------------------------------------------- #


def _events_path(root: Path, stream: str) -> Path:
    return root / "state" / "events" / f"{stream}.ndjson"


def _read_stream_since(root: Path, stream: str, since: datetime) -> list[dict]:
    """Read up to _MAX_LINES_PER_STREAM recent records from a stream since *since*.

    Returns records in chronological order (oldest first, newest last).
    String values longer than _PAYLOAD_STRING_CAP are truncated.
    """
    path = _events_path(root, stream)
    if not path.exists():
        return []

    since_iso = since.isoformat()
    matching: list[dict] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts", "")
                if ts >= since_iso:
                    matching.append(_truncate_strings(rec))
    except OSError:
        return []

    # Keep only the most recent _MAX_LINES_PER_STREAM entries.
    return matching[-_MAX_LINES_PER_STREAM:]


def _truncate_strings(obj: Any) -> Any:
    """Recursively truncate string values longer than _PAYLOAD_STRING_CAP."""
    if isinstance(obj, str):
        if len(obj) > _PAYLOAD_STRING_CAP:
            return obj[:_PAYLOAD_STRING_CAP] + "...[truncated]"
        return obj
    if isinstance(obj, dict):
        return {k: _truncate_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate_strings(v) for v in obj]
    return obj


def _read_prior_watcher_notes(root: Path, limit: int = _PRIOR_NOTES_LIMIT) -> list[dict]:
    """Return the last *limit* watcher notes, oldest first."""
    path = _events_path(root, _WATCHER_NOTES_STREAM)
    if not path.exists():
        return []
    notes: list[dict] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                notes.append(rec)
    except OSError:
        return []
    return notes[-limit:]


def _last_note_ts(root: Path) -> datetime | None:
    """Return the ``ts`` of the most recent prior watcher note, or None."""
    notes = _read_prior_watcher_notes(root, limit=1)
    if not notes:
        return None
    ts_str = notes[-1].get("ts")
    if not isinstance(ts_str, str):
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Prompt assembly
# --------------------------------------------------------------------------- #


def _build_user_message(
    *,
    persona_prompt: str,
    since: datetime,
    now: datetime,
    lookback_minutes: float,
    detector_results: dict[str, Any],
    raw_streams: dict[str, list[dict]],
    prior_notes: list[dict],
) -> str:
    """Assemble the full user message sent to the LLM.

    Order:
    1. Persona prompt (system-level role definition)
    2. Context header with timing metadata
    3. Prior watcher notes (continuity)
    4. Detector results with inline docstrings
    5. Raw stream excerpts (one section per stream)
    6. Instruction to return JSON
    """
    parts: list[str] = [
        persona_prompt.rstrip(),
        "",
        "---",
        "",
        "## Watcher context bundle",
        "",
        f"- **since_ts**: {since.isoformat()}",
        f"- **now_ts**: {now.isoformat()}",
        f"- **lookback_minutes**: {lookback_minutes:.1f}",
        "",
    ]

    # Prior watcher notes
    parts.append("### Prior watcher notes (oldest first, newest last)")
    parts.append("")
    if prior_notes:
        for note in prior_notes:
            ts_str = note.get("ts", "?")
            inner = note.get("note", {})
            summary = inner.get("summary", "?") if isinstance(inner, dict) else "?"
            escalated = inner.get("escalate_to_l2", False) if isinstance(inner, dict) else False
            esc_str = " [ESCALATED]" if escalated else ""
            parts.append(f"- `{ts_str}`{esc_str}: {summary}")
    else:
        parts.append("_(no prior notes — this is the first watcher run)_")
    parts.append("")

    # Detector results with docstrings
    parts.append("### Detector results")
    parts.append("")
    parts.append(
        "Each detector result is accompanied by its docstring so you "
        "understand what the data means. The `_docstring` key is the "
        "detector's Python docstring; `result` is the actual output."
    )
    parts.append("")
    for name, result in detector_results.items():
        doc = DETECTOR_DOCS.get(name, "(no docstring available)")
        parts.append(f"#### `{name}`")
        parts.append("")
        parts.append("**Docstring:**")
        parts.append("")
        # Indent the docstring for readability
        for line in (doc or "").splitlines():
            parts.append(f"    {line}")
        parts.append("")
        parts.append("**Result:**")
        parts.append("")
        parts.append("```json")
        parts.append(json.dumps(result, indent=2, default=str))
        parts.append("```")
        parts.append("")

    # Raw stream excerpts
    parts.append("### Raw signal stream excerpts (newest last, up to 200 lines per stream)")
    parts.append("")
    for stream_name, records in raw_streams.items():
        parts.append(f"#### `{stream_name}.ndjson` ({len(records)} records in window)")
        parts.append("")
        if records:
            parts.append("```json")
            for rec in records:
                parts.append(json.dumps(rec, default=str))
            parts.append("```")
        else:
            parts.append("_(empty — no events in this window)_")
        parts.append("")

    # Final instruction
    parts.append("---")
    parts.append("")
    parts.append(
        "Return ONLY the JSON object described in the output schema. "
        "No markdown fences, no prose before or after the JSON object."
    )

    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Sentinel / fallback
# --------------------------------------------------------------------------- #


def _sentinel_result(*, error: str) -> dict[str, Any]:
    return {
        "summary": "<watcher LLM failed>",
        "escalate_to_l2": False,
        "escalation_reason": None,
        "observations": [],
        "error": error,
    }


def _dry_run_result() -> dict[str, Any]:
    return {
        "summary": "<dry-run>",
        "escalate_to_l2": False,
        "escalation_reason": None,
        "observations": [],
    }


# --------------------------------------------------------------------------- #
# JSON schema for the watcher output
# --------------------------------------------------------------------------- #

_WATCHER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "escalate_to_l2", "escalation_reason", "observations"],
    "properties": {
        "summary": {"type": "string"},
        "escalate_to_l2": {"type": "boolean"},
        "escalation_reason": {"type": ["string", "null"]},
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["detector", "noteworthy"],
                "properties": {
                    "detector": {"type": "string"},
                    "noteworthy": {"type": ["string", "null"]},
                },
            },
        },
    },
}


# --------------------------------------------------------------------------- #
# LLM call with retry
# --------------------------------------------------------------------------- #


def _call_llm(
    *,
    persona_prompt: str,
    user_message: str,
    model_id: str,
    max_tokens: int,
) -> dict[str, Any]:
    """Call the LLM and parse JSON. Retries once on parse failure.

    On two consecutive failures, returns a sentinel result without raising.
    Uses the module-level ``text_run`` wrapper so tests can monkeypatch it.
    """
    # First attempt — text_run with schema requests JSON mode from the provider.
    try:
        result = text_run(
            "manager_watcher",
            user_message,
            model_id,
            schema=_WATCHER_SCHEMA,
            max_tokens=max_tokens,
        )
        if isinstance(result, dict):
            return result
        # result is a plain string (shouldn't happen with schema, but be defensive)
        parsed = json.loads(str(result))
        if isinstance(parsed, dict):
            return parsed
        return _sentinel_result(error=f"non-dict top-level result: {str(result)[:200]}")
    except json.JSONDecodeError as exc:
        first_error = repr(exc)
    except Exception as exc:  # noqa: BLE001
        # text_run raises RuntimeError when JSON parsing fails after all retries.
        first_error = repr(exc)
        # If text_run already did its internal retry loop and failed, return sentinel.
        return _sentinel_result(error=f"text_run_failed: {first_error}")

    # Second attempt — hint about the failure.
    retry_message = (
        f"{user_message}\n\n"
        "---\n\n"
        f"Your previous response was invalid JSON: {first_error}\n\n"
        "Return ONLY a valid JSON object matching the required schema. "
        "No markdown, no prose."
    )
    try:
        result = text_run(
            "manager_watcher",
            retry_message,
            model_id,
            schema=_WATCHER_SCHEMA,
            max_tokens=max_tokens,
        )
        if isinstance(result, dict):
            return result
        parsed = json.loads(str(result))
        if isinstance(parsed, dict):
            return parsed
        return _sentinel_result(error=f"retry non-dict: {str(result)[:200]}")
    except Exception as exc:  # noqa: BLE001
        return _sentinel_result(error=f"retry_failed: {repr(exc)}")


# --------------------------------------------------------------------------- #
# Main entry points
# --------------------------------------------------------------------------- #


def run_watcher_once(
    *,
    root: Path,
    now: datetime | None = None,
    lookback: timedelta = timedelta(minutes=15),
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one watcher cycle.

    1. Determines ``since`` from the last watcher note (clamped to
       ``now - lookback`` if no prior notes).
    2. Calls all 6 seed detectors with defaults derived from ``since``.
    3. Reads raw streams since ``since`` (capped at 200 lines each).
    4. Reads last 10 prior watcher notes.
    5. Builds the user message (persona prompt + bundled context).
    6. Calls the LLM (skipped in dry-run mode).
    7. Appends the result to ``state/events/watcher_notes.ndjson``.
    8. Returns the full result dict.

    Parameters
    ----------
    root:
        Factory root directory.
    now:
        Override the current time (useful for tests).
    lookback:
        Maximum lookback window when no prior notes exist.
    dry_run:
        If True, assembles the prompt but does not call the LLM.
        Prints the user message to stdout and returns a sentinel note.

    Returns
    -------
    dict
        The envelope written to ``watcher_notes.ndjson``.
    """
    from factory.model_router import max_output_tokens_for, route

    root = Path(root)
    now = now or datetime.now(UTC)

    # Determine since: last note ts, clamped to max lookback.
    earliest_allowed = now - lookback
    last_ts = _last_note_ts(root)
    if last_ts is None:
        since = earliest_allowed
    else:
        # Clamp: don't go further back than lookback, even if last note is old.
        since = max(last_ts, earliest_allowed)

    lookback_minutes = (now - since).total_seconds() / 60.0

    # Call detectors.
    detector_results: dict[str, Any] = {}

    # runs_failed_since
    try:
        detector_results["runs_failed_since"] = DETECTORS["runs_failed_since"](
            root=root, since=since
        )
    except Exception as exc:  # noqa: BLE001
        detector_results["runs_failed_since"] = {"error": repr(exc)}

    # retry_storm
    try:
        detector_results["retry_storm"] = DETECTORS["retry_storm"](
            root=root, since=since
        )
    except Exception as exc:  # noqa: BLE001
        detector_results["retry_storm"] = {"error": repr(exc)}

    # review_churn — surfaces stories cycling through review without
    # converging. Unlike retry_storm (failures only), this counts SUCCESSFUL
    # dev<->reviewer ping-pong, the green-but-non-converging loop that no
    # failure-based detector and no single 60s window can see.
    try:
        detector_results["review_churn"] = DETECTORS["review_churn"](
            root=root, since=since
        )
    except Exception as exc:  # noqa: BLE001
        detector_results["review_churn"] = {"error": repr(exc)}

    # cost_spike
    try:
        detector_results["cost_spike"] = DETECTORS["cost_spike"](root=root)
    except Exception as exc:  # noqa: BLE001
        detector_results["cost_spike"] = {"error": repr(exc)}

    # tick_duration_outliers
    try:
        detector_results["tick_duration_outliers"] = DETECTORS["tick_duration_outliers"](
            root=root, since=since
        )
    except Exception as exc:  # noqa: BLE001
        detector_results["tick_duration_outliers"] = {"error": repr(exc)}

    # state_distribution_skew
    try:
        detector_results["state_distribution_skew"] = DETECTORS["state_distribution_skew"](
            root=root, since=since
        )
    except Exception as exc:  # noqa: BLE001
        detector_results["state_distribution_skew"] = {"error": repr(exc)}

    # worktree_orphans — does not take since
    try:
        detector_results["worktree_orphans"] = DETECTORS["worktree_orphans"](root=root)
    except Exception as exc:  # noqa: BLE001
        detector_results["worktree_orphans"] = {"error": repr(exc)}

    # stalled_stories — ABSOLUTE liveness. Deliberately does NOT take ``since``:
    # it reads current DB state + last-tick time so a silently-stuck factory
    # (no events in the window → every other detector blind) still fires. A
    # non-empty ``alarms`` list is the loud signal the old window-only watcher
    # never produced.
    try:
        detector_results["stalled_stories"] = DETECTORS["stalled_stories"](root=root, now=now)
    except Exception as exc:  # noqa: BLE001
        detector_results["stalled_stories"] = {"error": repr(exc)}

    # placeholder_prompts — surfaces prompt-log records where a literal
    # placeholder string ("(fetched from GitHub by the chain", "(see {", etc.)
    # survived into the prompt sent to the LLM. Every record returned here is
    # a plumbing bug in a handler's prompt assembly and worth escalating; the
    # detector docstring (rendered into the user message by _build_user_message)
    # spells out the historical context for the LLM.
    try:
        detector_results["placeholder_prompts"] = DETECTORS["placeholder_prompts"](
            root=root, since=since
        )
    except Exception as exc:  # noqa: BLE001
        detector_results["placeholder_prompts"] = {"error": repr(exc)}

    # Read raw streams.
    raw_streams: dict[str, list[dict]] = {}
    for stream_name in _RAW_STREAMS:
        raw_streams[stream_name] = _read_stream_since(root, stream_name, since)

    # Read prior watcher notes.
    prior_notes = _read_prior_watcher_notes(root)

    # Load persona prompt.
    persona_prompt = _read_persona_prompt("manager_watcher")

    # Build user message.
    user_message = _build_user_message(
        persona_prompt=persona_prompt,
        since=since,
        now=now,
        lookback_minutes=lookback_minutes,
        detector_results=detector_results,
        raw_streams=raw_streams,
        prior_notes=prior_notes,
    )

    if dry_run:
        # Print the assembled prompt and return a sentinel note without calling LLM.
        print(user_message)
        note = _dry_run_result()
    else:
        # Call LLM.
        model_id = route("manager_watcher")
        max_tokens = max_output_tokens_for(model_id)
        note = _call_llm(
            persona_prompt=persona_prompt,
            user_message=user_message,
            model_id=model_id,
            max_tokens=max_tokens,
        )

    # Assemble envelope.
    envelope: dict[str, Any] = {
        "ts": now.isoformat(),
        "schema_version": _SCHEMA_VERSION,
        "event": _WATCHER_NOTES_STREAM,
        "lookback_minutes": round(lookback_minutes, 2),
        "since_ts": since.isoformat(),
        "note": note,
    }

    # Append to watcher_notes.ndjson.
    _append_watcher_note(root, envelope)

    return envelope


def _append_watcher_note(root: Path, envelope: dict[str, Any]) -> None:
    """Best-effort append of a watcher note to state/events/watcher_notes.ndjson."""
    path = _events_path(root, _WATCHER_NOTES_STREAM)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(envelope) + "\n")
    except Exception as exc:  # noqa: BLE001
        import sys

        print(f"[watcher] failed to append note: {exc}", file=sys.stderr)


def run_watcher_daemon(
    *,
    root: Path,
    interval_s: int = 60,
    max_iters: int | None = None,
    lookback: timedelta = timedelta(minutes=15),
    trigger_l2: bool = True,
    trigger_l3: bool = True,
    auto_apply: bool = True,
    circuit_breaker_interval_min: int = 30,
) -> None:
    """Loop ``run_watcher_once`` every ``interval_s`` seconds.

    Runs until interrupted by SIGINT (KeyboardInterrupt) or until
    ``max_iters`` iterations have completed (when provided — useful
    for tests).

    When L1 produces a note with ``escalate_to_l2=true``, immediately
    triggers one L2 summarizer iteration (``run_summarizer_once``) unless
    ``trigger_l2=False`` (the ``--no-l2`` flag).

    When L2 produces a concern with ``escalate_to_l3=true``, immediately
    triggers one L3 diagnostician iteration (``run_diagnostician_once``)
    unless ``trigger_l3=False`` (the ``--no-l3`` flag).

    Parameters
    ----------
    root:
        Factory root directory.
    interval_s:
        Seconds to sleep between watcher runs.
    max_iters:
        If set, exit after this many iterations. If None, run forever
        until KeyboardInterrupt.
    lookback:
        Maximum lookback window passed to each ``run_watcher_once`` call.
    trigger_l2:
        If True (default), immediately invoke L2 when L1 escalates.
        Set to False to suppress L2 (useful for testing L1 in isolation).
    trigger_l3:
        If True (default), immediately invoke L3 when L2 escalates.
        Set to False to suppress L3 (useful for testing L2 in isolation).
    auto_apply:
        If True (default -- MVP ON), immediately invoke the L4 apply pipeline
        when L3 produces a proposal.  Set to False (``--no-auto-apply``) to
        skip L4 and let the operator run ``factory manager apply`` manually.
    circuit_breaker_interval_min:
        How often (minutes) to run circuit-breaker ``check_and_trip`` if there
        are tracked manager commits.  0 disables periodic checks.  Default: 30.
    """
    import sys

    iterations = 0
    _last_cb_check: datetime | None = None
    _cb_interval = timedelta(minutes=circuit_breaker_interval_min) if circuit_breaker_interval_min > 0 else None
    print(
        f"[watcher] starting daemon (interval_s={interval_s}, "
        f"trigger_l2={trigger_l2}, trigger_l3={trigger_l3}, "
        f"auto_apply={auto_apply}, "
        f"circuit_breaker_interval_min={circuit_breaker_interval_min})",
        file=sys.stderr,
    )
    if auto_apply:
        print(
            "[watcher] auto_apply=ON -- L4 will automatically apply safe proposals. "
            "Pass --no-auto-apply to disable.",
            file=sys.stderr,
        )
    try:
        while True:
            # Phase 8 (Phase 7 reviewer note): check halt state before each
            # iteration so the daemon skips LLM work while the factory is
            # halted.  The circuit breaker is also checked, but daemons still
            # RUN when the breaker is tripped — detection and proposal
            # generation are still useful; only the L4 apply pipeline is halted.
            try:
                from factory.manager.halt import is_halted as _is_halted
                if _is_halted(root=root):
                    print(
                        "[watcher] factory halted: skipping iteration",
                        file=sys.stderr,
                    )
                    iterations += 1
                    if max_iters is not None and iterations >= max_iters:
                        print(
                            f"[watcher] reached max_iters={max_iters}, stopping.",
                            file=sys.stderr,
                        )
                        break
                    time.sleep(interval_s)
                    continue
            except Exception as _halt_exc:  # noqa: BLE001
                print(
                    f"[watcher] WARNING: halt-check failed: {_halt_exc!r}; continuing (fail-open)",
                    file=sys.stderr,
                )

            # Log circuit-breaker state if tripped (informational only — daemons keep running).
            try:
                from factory.manager.circuit_breaker import is_tripped as _cb_is_tripped
                if _cb_is_tripped(root=root):
                    print(
                        "[watcher] NOTE: circuit breaker is tripped; L4 apply is halted. "
                        "Detection and proposals continue.",
                        file=sys.stderr,
                    )
            except Exception:  # noqa: BLE001
                pass

            try:
                result = run_watcher_once(root=root, lookback=lookback)
                note = result.get("note", {})
                summary = note.get("summary", "") if isinstance(note, dict) else ""
                escalated = note.get("escalate_to_l2", False) if isinstance(note, dict) else False
                esc_tag = " [ESCALATE→L2]" if escalated else ""
                print(
                    f"[watcher] {result.get('ts', '?')}{esc_tag}: {summary[:120]}",
                    file=sys.stderr,
                )
                # Immediately trigger L2 if L1 escalated and trigger_l2 is enabled.
                if escalated and trigger_l2:
                    print("[watcher] triggering immediate L2 summarizer run...", file=sys.stderr)
                    l2_concern_path: str | None = None
                    try:
                        from factory.manager.summarizer import run_summarizer_once

                        l2_result = run_summarizer_once(root=root)
                        if l2_result is None:
                            print("[watcher] L2: no flagged notes found (possible race).", file=sys.stderr)
                        else:
                            l2_title = l2_result.get("title", "?")
                            l2_urgency = l2_result.get("urgency", "?")
                            l2_esc = l2_result.get("escalate_to_l3", False)
                            l2_esc_tag = " [ESCALATE→L3]" if l2_esc else ""
                            print(
                                f"[watcher] L2 concern={l2_title!r} urgency={l2_urgency}{l2_esc_tag}",
                                file=sys.stderr,
                            )
                            # Store the concern path for L3 trigger below.
                            l2_concern_path = l2_result.get("concern_path")
                            # Immediately trigger L3 if L2 escalated and trigger_l3 is enabled.
                            if l2_esc and trigger_l3 and l2_concern_path:
                                print(
                                    "[watcher] triggering immediate L3 diagnostician run...",
                                    file=sys.stderr,
                                )
                                try:
                                    from factory.manager.diagnostician import (
                                        run_diagnostician_once,
                                    )

                                    l3_result = run_diagnostician_once(
                                        root=root,
                                        concern_path=Path(l2_concern_path),
                                    )
                                    if l3_result is None:
                                        print(
                                            "[watcher] L3: no proposal produced.",
                                            file=sys.stderr,
                                        )
                                    else:
                                        l3_title = l3_result.get("concern_title", "?")
                                        l3_class = l3_result.get("target_class", "?")
                                        l3_esc = l3_result.get("escalate_to_human", False)
                                        l3_esc_tag = " [ESCALATE->HUMAN]" if l3_esc else ""
                                        print(
                                            f"[watcher] L3 concern={l3_title!r} "
                                            f"target_class={l3_class}{l3_esc_tag}",
                                            file=sys.stderr,
                                        )
                                        # Immediately trigger L4 if auto_apply is enabled.
                                        if auto_apply:
                                            l3_proposal_path = l3_result.get("proposal_path")
                                            print(
                                                "[watcher] triggering immediate L4 apply run...",
                                                file=sys.stderr,
                                            )
                                            try:
                                                from factory.manager.apply import (
                                                    apply_manager_proposals,
                                                )

                                                l4_result = apply_manager_proposals(
                                                    root=root,
                                                    proposal_path=Path(l3_proposal_path)
                                                    if l3_proposal_path
                                                    else None,
                                                )
                                                print(
                                                    f"[watcher] L4 apply: "
                                                    f"processed={l4_result.get('processed', 0)} "
                                                    f"safe_applied={l4_result.get('safe_applied', 0)} "
                                                    f"risky_opened={l4_result.get('risky_opened', 0)} "
                                                    f"forbidden={l4_result.get('forbidden', 0)}",
                                                    file=sys.stderr,
                                                )
                                            except Exception as l4_exc:  # noqa: BLE001
                                                print(
                                                    f"[watcher] L4 apply trigger failed: {l4_exc!r}",
                                                    file=sys.stderr,
                                                )
                                except Exception as l3_exc:  # noqa: BLE001
                                    print(
                                        f"[watcher] L3 trigger failed: {l3_exc!r}",
                                        file=sys.stderr,
                                    )
                    except Exception as l2_exc:  # noqa: BLE001
                        print(f"[watcher] L2 trigger failed: {l2_exc!r}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"[watcher] run_watcher_once raised: {exc!r}", file=sys.stderr)

            iterations += 1
            if max_iters is not None and iterations >= max_iters:
                print(
                    f"[watcher] reached max_iters={max_iters}, stopping.",
                    file=sys.stderr,
                )
                break

            # Phase 8: periodic circuit-breaker check (if enabled and due).
            if _cb_interval is not None:
                _cb_now = datetime.now(UTC)
                if _last_cb_check is None or (_cb_now - _last_cb_check) >= _cb_interval:
                    _last_cb_check = _cb_now
                    try:
                        from factory.manager.circuit_breaker import (
                            _load_manager_commits as _cb_load_commits,
                        )
                        from factory.manager.circuit_breaker import (
                            check_and_trip as _cb_check,
                        )

                        if _cb_load_commits(root):  # only run if there are tracked commits
                            print(
                                "[watcher] running periodic circuit-breaker check...",
                                file=sys.stderr,
                            )
                            _cb_result = _cb_check(root=root)
                            if _cb_result is not None:
                                print(
                                    f"[watcher] circuit breaker TRIPPED: "
                                    f"regression={_cb_result.get('regression_commit', '?')[:12]!r} "
                                    f"halt_until={_cb_result.get('halt_until', '?')}",
                                    file=sys.stderr,
                                )
                    except Exception as _cb_exc:  # noqa: BLE001
                        print(
                            f"[watcher] WARNING: circuit-breaker check failed: {_cb_exc!r}",
                            file=sys.stderr,
                        )

            time.sleep(interval_s)
    except KeyboardInterrupt:
        print("\n[watcher] interrupted, shutting down.", file=sys.stderr)


__all__ = [
    "run_watcher_once",
    "run_watcher_daemon",
]
