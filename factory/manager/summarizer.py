"""factory.manager.summarizer — L2 Summarizer agent (Phase 4).

The summarizer is the second LLM-in-the-loop component of the FMS.  It runs
every 3 minutes (or immediately when L1 escalates), reads watcher notes that
L1 flagged with ``escalate_to_l2=true``, assembles the underlying signals and
detector docstrings, and asks a mid-tier LLM to produce a structured *concern
document* — an artifact with explicit evidence and an urgency rating.

Architecture note
-----------------
This module is *plumbing*.  It assembles context, calls the LLM, and writes
the result.  No anomaly judgment lives here — judgment lives in
``factory/personas/manager_summarizer.md``.

Public API
----------
* ``run_summarizer_once`` — one summarizer invocation; returns the full
  result dict or ``None`` if there are no flagged notes.
* ``run_summarizer_daemon`` — loops ``run_summarizer_once`` every N seconds.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from factory.manager.detectors import DETECTOR_DOCS

# ---------------------------------------------------------------------------
# Lazily-imported helpers — at module level so tests can monkeypatch via
# ``factory.manager.summarizer.text_run`` etc.
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


# Streams the summarizer reads raw lines from.
_RAW_STREAMS = ("runs", "ticks", "queue", "webhooks", "git", "spend")

# Cap per stream (recent lines only).
_MAX_LINES_PER_STREAM = 200

# Cap on any single payload string value (chars).
_PAYLOAD_STRING_CAP = 500

# Maximum excerpt chars for evidence items.
_EVIDENCE_EXCERPT_CAP = 300

# Stream names.
_WATCHER_NOTES_STREAM = "watcher_notes"
_CONCERNS_STREAM = "concerns"

# How many prior concerns to include for continuity.
_PRIOR_CONCERNS_LIMIT = 5

# Schema version emitted by this module.
_SCHEMA_VERSION = 1

# Slug character validation pattern.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,58}[a-z0-9]$|^[a-z0-9]$")


# --------------------------------------------------------------------------- #
# Helpers — path and stream reading
# --------------------------------------------------------------------------- #


def _events_path(root: Path, stream: str) -> Path:
    return root / "state" / "events" / f"{stream}.ndjson"


def _concerns_dir(root: Path) -> Path:
    return root / "state" / "concerns"


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


def _read_stream_between(
    root: Path, stream: str, start: datetime, end: datetime
) -> list[dict]:
    """Read records from a stream between *start* and *end* (inclusive).

    Returns at most _MAX_LINES_PER_STREAM records, newest last.
    String values are truncated.
    """
    path = _events_path(root, stream)
    if not path.exists():
        return []

    start_iso = start.isoformat()
    end_iso = end.isoformat()
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
                if start_iso <= ts <= end_iso:
                    matching.append(_truncate_strings(rec))
    except OSError:
        return []

    return matching[-_MAX_LINES_PER_STREAM:]


def _read_all_watcher_notes(root: Path) -> list[dict]:
    """Return all watcher notes from watcher_notes.ndjson, oldest first."""
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
    return notes


def _last_concern_ts(root: Path) -> datetime | None:
    """Return the ts of the most recent concern, or None.

    Reads state/events/concerns.ndjson for the last emitted concern_emitted
    event. This tells us how far back to look for new flagged notes.
    """
    path = _events_path(root, _CONCERNS_STREAM)
    if not path.exists():
        return None
    last_ts: str | None = None
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
                ts = rec.get("ts")
                if isinstance(ts, str):
                    last_ts = ts
    except OSError:
        return None
    if last_ts is None:
        return None
    try:
        dt = datetime.fromisoformat(last_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _read_prior_concerns(root: Path, limit: int = _PRIOR_CONCERNS_LIMIT) -> list[dict]:
    """Return the last *limit* concern documents from state/concerns/, newest last."""
    concerns_dir = _concerns_dir(root)
    if not concerns_dir.exists():
        return []
    files = sorted(concerns_dir.glob("*.json"))
    files = files[-limit:]
    concerns: list[dict] = []
    for f in files:
        try:
            concerns.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return concerns


# --------------------------------------------------------------------------- #
# Prompt assembly
# --------------------------------------------------------------------------- #


def _build_user_message(
    *,
    persona_prompt: str,
    since: datetime,
    now: datetime,
    flagged_notes: list[dict],
    signals_by_window: list[dict[str, list[dict]]],
    prior_concerns: list[dict],
) -> str:
    """Assemble the full user message sent to the L2 LLM.

    Order:
    1. Persona prompt
    2. Context header with timing metadata
    3. Prior concerns (continuity)
    4. Detector docstrings (so L2 understands the data shape)
    5. Flagged watcher notes with their detector observations
    6. Underlying signals per note window
    7. Instruction to return JSON
    """
    parts: list[str] = [
        persona_prompt.rstrip(),
        "",
        "---",
        "",
        "## Summarizer context bundle",
        "",
        f"- **since_ts**: {since.isoformat()}",
        f"- **now_ts**: {now.isoformat()}",
        f"- **flagged_note_count**: {len(flagged_notes)}",
        "",
    ]

    # Prior concerns for continuity
    parts.append("### Prior concerns (last up to 5, oldest first)")
    parts.append("")
    if prior_concerns:
        for concern in prior_concerns:
            title = concern.get("title", "?")
            urgency = concern.get("urgency", "?")
            ts = concern.get("concern_path", "?")
            desc = concern.get("description", "")[:200]
            parts.append(f"#### Prior concern: `{title}` (urgency={urgency})")
            parts.append("")
            parts.append(f"_{desc}_")
            parts.append(f"_(path: {ts})_")
            parts.append("")
    else:
        parts.append("_(no prior concerns — this may be the first L2 run)_")
    parts.append("")

    # Detector docstrings
    parts.append("### Detector docstrings (what each detector field means)")
    parts.append("")
    for name, doc in DETECTOR_DOCS.items():
        parts.append(f"#### `{name}`")
        parts.append("")
        for line in (doc or "").splitlines():
            parts.append(f"    {line}")
        parts.append("")

    # Flagged watcher notes
    parts.append("### Flagged watcher notes (these triggered this L2 run)")
    parts.append("")
    for i, note_env in enumerate(flagged_notes):
        ts_str = note_env.get("ts", "?")
        since_str = note_env.get("since_ts", "?")
        inner = note_env.get("note", {})
        summary = inner.get("summary", "?") if isinstance(inner, dict) else "?"
        esc_reason = inner.get("escalation_reason", "") if isinstance(inner, dict) else ""
        observations = inner.get("observations", []) if isinstance(inner, dict) else []

        parts.append(f"#### Note {i + 1} (ts={ts_str}, since={since_str})")
        parts.append("")
        parts.append(f"**Summary:** {summary}")
        parts.append("")
        if esc_reason:
            parts.append(f"**Escalation reason:** {esc_reason}")
            parts.append("")
        if observations:
            parts.append("**Detector observations from this note:**")
            parts.append("")
            parts.append("```json")
            parts.append(json.dumps(observations, indent=2, default=str))
            parts.append("```")
            parts.append("")

        # Underlying signals for this window
        if i < len(signals_by_window):
            window_signals = signals_by_window[i]
            parts.append("**Underlying signals for this window (streams):**")
            parts.append("")
            for stream_name, records in window_signals.items():
                parts.append(
                    f"##### `{stream_name}.ndjson` ({len(records)} records in window)"
                )
                parts.append("")
                if records:
                    parts.append("```json")
                    for rec in records:
                        parts.append(json.dumps(rec, default=str))
                    parts.append("```")
                else:
                    parts.append("_(empty)_")
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
# JSON schema for the L2 output
# --------------------------------------------------------------------------- #

_SUMMARIZER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "title",
        "description",
        "evidence",
        "proposed_area",
        "urgency",
        "escalate_to_l3",
        "escalation_reason",
    ],
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["run", "tick", "watcher_note", "detector_observation"],
                    },
                    "id": {"type": ["integer", "null"]},
                    "ts": {"type": "string"},
                    "excerpt": {"type": "string"},
                    "tick_id": {"type": "string"},
                    "duration_s": {"type": ["number", "null"]},
                    "summary_excerpt": {"type": "string"},
                    "detector": {"type": "string"},
                },
            },
        },
        "proposed_area": {
            "type": "string",
            "enum": [
                "prompt",
                "persona_settings",
                "dispatch_code",
                "detector_tool",
                "observability",
                "unknown",
            ],
        },
        "urgency": {"type": "string", "enum": ["continue", "warn", "halt"]},
        "escalate_to_l3": {"type": "boolean"},
        "escalation_reason": {"type": ["string", "null"]},
    },
}


# --------------------------------------------------------------------------- #
# Sentinel / fallback
# --------------------------------------------------------------------------- #


def _sentinel_concern(*, error: str) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "title": "l2-parse-failure",
        "description": f"L2 LLM failed to produce parseable output: {error}",
        "evidence": [],
        "proposed_area": "unknown",
        "urgency": "continue",
        "escalate_to_l3": False,
        "escalation_reason": None,
        "error": error,
    }


def _dry_run_concern() -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "title": "dry-run-sentinel",
        "description": "<dry-run — LLM not called>",
        "evidence": [],
        "proposed_area": "unknown",
        "urgency": "continue",
        "escalate_to_l3": False,
        "escalation_reason": None,
    }


# --------------------------------------------------------------------------- #
# LLM call with retry
# --------------------------------------------------------------------------- #


def _call_llm(
    *,
    user_message: str,
    model_id: str,
    max_tokens: int,
) -> dict[str, Any]:
    """Call the L2 LLM and parse JSON. Retries once on parse failure.

    On two consecutive failures, returns a sentinel concern without raising.
    """
    # First attempt.
    try:
        result = text_run(
            "manager_summarizer",
            user_message,
            model_id,
            schema=_SUMMARIZER_SCHEMA,
            max_tokens=max_tokens,
        )
        if isinstance(result, dict):
            return result
        parsed = json.loads(str(result))
        if isinstance(parsed, dict):
            return parsed
        return _sentinel_concern(error=f"non-dict top-level result: {str(result)[:200]}")
    except json.JSONDecodeError as exc:
        first_error = repr(exc)
    except Exception as exc:  # noqa: BLE001
        first_error = repr(exc)
        return _sentinel_concern(error=f"text_run_failed: {first_error}")

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
            "manager_summarizer",
            retry_message,
            model_id,
            schema=_SUMMARIZER_SCHEMA,
            max_tokens=max_tokens,
        )
        if isinstance(result, dict):
            return result
        parsed = json.loads(str(result))
        if isinstance(parsed, dict):
            return parsed
        return _sentinel_concern(error=f"retry non-dict: {str(result)[:200]}")
    except Exception as exc:  # noqa: BLE001
        return _sentinel_concern(error=f"retry_failed: {repr(exc)}")


# --------------------------------------------------------------------------- #
# File writing
# --------------------------------------------------------------------------- #


def _sanitize_slug(title: str) -> str:
    """Convert an LLM-emitted title to a safe filename slug.

    Keeps only [a-z0-9-], collapses runs of non-word chars to '-',
    strips leading/trailing dashes, truncates at 60 chars.
    """
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9\-]+", "-", slug)
    slug = slug.strip("-")
    slug = slug[:60].rstrip("-")
    return slug or "unnamed-concern"


def _write_concern(root: Path, concern: dict[str, Any], now: datetime) -> Path:
    """Write a concern document to state/concerns/<ts>-<slug>.json.

    Also appends a compact concern_emitted event to
    state/events/concerns.ndjson.

    Returns the path written.
    """
    import sys

    slug = _sanitize_slug(concern.get("title", "unnamed"))
    ts_prefix = now.strftime("%Y%m%dT%H%M%S")
    filename = f"{ts_prefix}-{slug}.json"

    concerns_dir = _concerns_dir(root)
    try:
        concerns_dir.mkdir(parents=True, exist_ok=True)
        concern_path = concerns_dir / filename
        concern_doc = {"schema_version": _SCHEMA_VERSION, **concern}
        concern_path.write_text(json.dumps(concern_doc, indent=2, default=str), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[summarizer] failed to write concern file: {exc}", file=sys.stderr)
        concern_path = concerns_dir / filename  # still return the intended path

    # Append compact event to concerns.ndjson.
    event_path = _events_path(root, _CONCERNS_STREAM)
    event: dict[str, Any] = {
        "ts": now.isoformat(),
        "schema_version": _SCHEMA_VERSION,
        "event": "concern_emitted",
        "title": concern.get("title", ""),
        "urgency": concern.get("urgency", "continue"),
        "escalate_to_l3": concern.get("escalate_to_l3", False),
        "concern_path": str(concern_path),
    }
    try:
        event_path.parent.mkdir(parents=True, exist_ok=True)
        with event_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[summarizer] failed to append concern event: {exc}", file=sys.stderr)

    return concern_path


# --------------------------------------------------------------------------- #
# Main entry points
# --------------------------------------------------------------------------- #


def run_summarizer_once(
    *,
    root: Path,
    now: datetime | None = None,
    lookback: timedelta = timedelta(hours=2),
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """Run one summarizer cycle.

    1. Computes ``since`` from the last concern emitted (clamped to
       ``now - lookback`` if no prior concerns).
    2. Reads watcher_notes.ndjson and finds notes with ``escalate_to_l2=true``
       since ``since``.
    3. If there are NO flagged notes, returns ``None`` (no LLM call).
    4. For each flagged note, gathers the underlying signals between the
       prior note's ts and this note's ts.
    5. Reads the last 5 prior concerns for continuity.
    6. Builds the L2 user message.
    7. Calls the LLM (skipped in dry-run mode).
    8. Parses the JSON; on failure returns a sentinel concern.
    9. Writes the concern to state/concerns/<ts>-<slug>.json and appends
       a line to state/events/concerns.ndjson.
    10. Returns the parsed concern dict plus a ``concern_path`` field.

    Parameters
    ----------
    root:
        Factory root directory.
    now:
        Override the current time (useful for tests).
    lookback:
        Maximum lookback window when no prior concerns exist.
    dry_run:
        If True, assembles the prompt but does not call the LLM.
        Prints the user message to stdout and returns a sentinel concern.

    Returns
    -------
    dict | None
        The concern dict (plus ``concern_path``) if a concern was produced,
        or ``None`` if there were no flagged watcher notes to process.
    """
    from factory.model_router import max_output_tokens_for, route

    root = Path(root)
    now = now or datetime.now(UTC)

    # Determine since: last concern ts, clamped to max lookback.
    earliest_allowed = now - lookback
    last_ts = _last_concern_ts(root)
    if last_ts is None:
        since = earliest_allowed
    else:
        since = max(last_ts, earliest_allowed)

    # Read all watcher notes and find flagged ones since `since`.
    all_notes = _read_all_watcher_notes(root)
    since_iso = since.isoformat()
    flagged_notes: list[dict] = []
    for note_env in all_notes:
        ts_str = note_env.get("ts", "")
        if not isinstance(ts_str, str) or ts_str < since_iso:
            continue
        inner = note_env.get("note", {})
        if isinstance(inner, dict) and inner.get("escalate_to_l2"):
            flagged_notes.append(note_env)

    if not flagged_notes:
        return None

    # For each flagged note, gather the underlying signals.
    # The time window for each note is [prior_note.ts, this_note.ts].
    signals_by_window: list[dict[str, list[dict]]] = []

    for note_env in flagged_notes:
        note_ts_str = note_env.get("ts", "")
        # Find the prior note's ts by looking at the full list.
        idx = None
        for i, env in enumerate(all_notes):
            if env.get("ts") == note_ts_str:
                idx = i
                break

        if idx is not None and idx > 0:
            prior_ts_str = all_notes[idx - 1].get("ts", since_iso)
        else:
            prior_ts_str = since_iso

        try:
            window_start = datetime.fromisoformat(prior_ts_str)
            if window_start.tzinfo is None:
                window_start = window_start.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            window_start = since

        try:
            window_end = datetime.fromisoformat(note_ts_str)
            if window_end.tzinfo is None:
                window_end = window_end.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            window_end = now

        window_signals: dict[str, list[dict]] = {}
        for stream_name in _RAW_STREAMS:
            window_signals[stream_name] = _read_stream_between(
                root, stream_name, window_start, window_end
            )
        signals_by_window.append(window_signals)

    # Read prior concerns.
    prior_concerns = _read_prior_concerns(root)

    # Load persona prompt.
    persona_prompt = _read_persona_prompt("manager_summarizer")

    # Build user message.
    user_message = _build_user_message(
        persona_prompt=persona_prompt,
        since=since,
        now=now,
        flagged_notes=flagged_notes,
        signals_by_window=signals_by_window,
        prior_concerns=prior_concerns,
    )

    if dry_run:
        print(user_message)
        concern = _dry_run_concern()
    else:
        model_id = route("manager_summarizer")
        max_tokens = max_output_tokens_for(model_id)
        concern = _call_llm(
            user_message=user_message,
            model_id=model_id,
            max_tokens=max_tokens,
        )

    # Write the concern.
    concern_path = _write_concern(root, concern, now)
    concern["concern_path"] = str(concern_path)

    return concern


def run_summarizer_daemon(
    *,
    root: Path,
    interval_s: int = 180,
    max_iters: int | None = None,
    lookback: timedelta = timedelta(hours=2),
) -> None:
    """Loop ``run_summarizer_once`` every ``interval_s`` seconds.

    Runs until interrupted by SIGINT (KeyboardInterrupt) or until
    ``max_iters`` iterations have completed (when provided — useful
    for tests).

    Parameters
    ----------
    root:
        Factory root directory.
    interval_s:
        Seconds to sleep between summarizer runs. Default 180 (3 min)
        — slower than L1 because L2 is more expensive.
    max_iters:
        If set, exit after this many iterations. If None, run forever.
    lookback:
        Maximum lookback window passed to each ``run_summarizer_once`` call.
    """
    import sys

    iterations = 0
    print(f"[summarizer] starting daemon (interval_s={interval_s})", file=sys.stderr)
    try:
        while True:
            try:
                result = run_summarizer_once(root=root, lookback=lookback)
                if result is None:
                    print("[summarizer] no flagged notes, skipping.", file=sys.stderr)
                else:
                    title = result.get("title", "?")
                    urgency = result.get("urgency", "?")
                    escalate = result.get("escalate_to_l3", False)
                    esc_tag = " [ESCALATE→L3]" if escalate else ""
                    print(
                        f"[summarizer] concern={title!r} urgency={urgency}{esc_tag}",
                        file=sys.stderr,
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"[summarizer] run_summarizer_once raised: {exc!r}", file=sys.stderr)

            iterations += 1
            if max_iters is not None and iterations >= max_iters:
                print(
                    f"[summarizer] reached max_iters={max_iters}, stopping.",
                    file=sys.stderr,
                )
                break

            time.sleep(interval_s)
    except KeyboardInterrupt:
        print("\n[summarizer] interrupted, shutting down.", file=sys.stderr)


__all__ = [
    "run_summarizer_once",
    "run_summarizer_daemon",
]
