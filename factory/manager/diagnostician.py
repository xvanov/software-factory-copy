"""factory.manager.diagnostician — L3 Diagnostician agent (Phase 5).

The diagnostician is the frontier-tier LLM in the FMS escalation chain.
It runs only when L2 (the Summarizer) has emitted a concern with
``escalate_to_l3=true``.  It reads the concern, pre-loads relevant factory
source files, and asks a frontier model to produce a concrete proposal with
a unified-diff patch.

Architecture note
-----------------
This module is *plumbing*.  It assembles context, calls the LLM, and writes
the result.  No anomaly judgment lives here — judgment lives in
``factory/personas/manager_diagnostician.md``.

The only Python "decision" in this module is the ``_pre_load_source``
helper, which selects which files to pre-load based on the concern's
``proposed_area`` field.  This is transparent context selection, not
judgment.

Public API
----------
* ``run_diagnostician_once`` — one diagnostician invocation; returns the
  full proposal dict or ``None`` if no unprocessed concerns exist.
* ``run_diagnostician_daemon`` — loops ``run_diagnostician_once`` every N
  seconds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Lazily-imported helpers — at module level so tests can monkeypatch via
# ``factory.manager.diagnostician.text_run`` etc.
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Schema version emitted by this module.
_SCHEMA_VERSION = 1

# Per-file content cap when pre-loading source files (bytes → chars).
_SOURCE_FILE_CAP = 16 * 1024  # 16 KB per file

# Total bundle cap before we warn (chars).
_BUNDLE_TOTAL_CAP = 100 * 1024  # 100 KB

# factory/chain/ holds the orchestrator/merge/git/recovery logic where most
# dispatch_code bugs actually live, but several files there (handlers.py,
# orchestrator.py) are much larger than the generic 16KB cap allows. Give
# chain/ files a wider per-file cap and a dedicated bundle budget so the L3
# diagnostician's source bundle can include the whole directory instead of a
# hardcoded three-file allowlist that omitted auto_merge.py, worktree.py,
# branch.py, rollback.py and recovery.py entirely.
# Wide enough that the largest chain file (handlers.py, ~137KB) loads WHOLE —
# a partial handlers.py was worse than useless: L3 would see a truncated file
# and still not find the buggy function. The bundle budget fits the full
# high-signal set plus headroom (~100K tokens for gpt-5.3-codex's context).
_CHAIN_FILE_CAP = 160 * 1024  # 160 KB per file
_DISPATCH_CODE_BUNDLE_CAP = 400 * 1024  # 400 KB for the dispatch_code chain/ loading budget
# The final bundle-total enforcement (see bottom of _pre_load_source) otherwise
# re-shrinks EVERY file to fit _BUNDLE_TOTAL_CAP (100KB), which would truncate
# handlers.py back down to a few KB and defeat the widened dispatch_code/unknown
# bundle. Chain-loading areas get this larger enforcement ceiling instead, sized
# to hold the full priority set (~278KB) + glob fill + context modules with
# headroom. ~140K tokens — within gpt-5.3-codex's context window.
_WIDE_BUNDLE_TOTAL_CAP = 560 * 1024
# proposed_area values that load persona/detector/observability files (small,
# capped at 100KB deliberately — e.g. prompt_edit loads ALL personas ~147KB and
# is MEANT to be trimmed). Everything else (dispatch_code + the unknown/else
# branch) loads chain/ source and uses the wide ceiling.
_NARROW_BUNDLE_AREAS = frozenset(
    {"prompt", "prompt_edit", "persona_settings", "detector_tool", "observability"}
)

# Slug character validation pattern.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,58}[a-z0-9]$|^[a-z0-9]$")


# ---------------------------------------------------------------------------
# JSON schema for the L3 diagnostician output
# ---------------------------------------------------------------------------

_DIAGNOSTICIAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "concern_title",
        "diagnosis",
        "proposal",
        "target_class",
        "escalate_to_human",
        "escalation_reason",
    ],
    "properties": {
        "concern_title": {"type": "string"},
        "diagnosis": {"type": "string"},
        "proposal": {
            "type": "object",
            "required": [
                "kind",
                "target",
                "rationale",
                "suggested_patch",
                "verification",
                "confidence",
            ],
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "prompt_edit",
                        "persona_settings",
                        "dispatch_code",
                        "detector_tool",
                        "observability",
                        "doc_update",
                    ],
                },
                "target": {"type": "string"},
                "rationale": {"type": "string"},
                "suggested_patch": {"type": "string"},
                "verification": {"type": "string"},
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                },
            },
        },
        "target_class": {
            "type": "string",
            "enum": [
                "prompt_edit",
                "persona_settings",
                "dispatch_code",
                "detector_tool",
                "escalate_to_human",
            ],
        },
        "escalate_to_human": {"type": "boolean"},
        "escalation_reason": {"type": ["string", "null"]},
        # Phase 7: halt-request fields (optional).  Only L3 may set these.
        # When request_halt=true, halt_reason MUST be a non-empty string.
        "request_halt": {"type": "boolean", "default": False},
        "halt_reason": {"type": ["string", "null"]},
    },
}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _proposals_dir(root: Path) -> Path:
    return root / "state" / "manager_proposals"


def _concerns_dir(root: Path) -> Path:
    return root / "state" / "concerns"


def _proposals_event_path(root: Path) -> Path:
    return root / "state" / "events" / "proposals.ndjson"


# ---------------------------------------------------------------------------
# Pre-load source files by proposed_area
# ---------------------------------------------------------------------------


def _pre_load_source(
    proposed_area: str,
    *,
    factory_dir: Path,
    root: Path | None = None,
) -> dict[str, str]:
    """Return {relative_path: content} for source files relevant to proposed_area.

    Caps each file at ``_SOURCE_FILE_CAP`` chars.  Warns (to stderr) if the
    total bundle exceeds ``_BUNDLE_TOTAL_CAP`` chars — the LLM may receive a
    degraded context.

    Parameters
    ----------
    proposed_area:
        The L2 concern's ``proposed_area`` field.  One of:
        ``prompt``, ``prompt_edit``, ``persona_settings``, ``dispatch_code``,
        ``detector_tool``, ``observability``, ``unknown``.
    factory_dir:
        Absolute path to the ``factory/`` directory (the source root).
    root:
        Optional override for the software_factory_root used when locating
        self-context modules under ``apps/factory/context/modules/``.  If
        None, defaults to ``factory_dir.parent`` (the production layout where
        ``factory/`` lives at the repo root).
    """
    files: dict[str, str] = {}

    def _read_file_capped(abs_path: Path, cap: int) -> str:
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        if len(text) > cap:
            text = text[:cap] + f"\n...[truncated at {cap // 1024}KB]"
        return text

    def _read_file(abs_path: Path) -> str:
        return _read_file_capped(abs_path, _SOURCE_FILE_CAP)

    def _rel(abs_path: Path) -> str:
        """Return path relative to factory_dir.parent (the repo root)."""
        try:
            return str(abs_path.relative_to(factory_dir.parent))
        except ValueError:
            return str(abs_path)

    if proposed_area in ("prompt", "prompt_edit"):
        personas_dir = factory_dir / "personas"
        for p in sorted(personas_dir.glob("*.md")):
            files[_rel(p)] = _read_file(p)

    elif proposed_area == "persona_settings":
        routes_yaml = factory_dir / "routes.yaml"
        if routes_yaml.exists():
            files[_rel(routes_yaml)] = _read_file(routes_yaml)
        personas_dir = factory_dir / "personas"
        for p in sorted(personas_dir.glob("*.md")):
            files[_rel(p)] = _read_file(p)

    elif proposed_area == "dispatch_code":
        # Bugs in this area live throughout factory/chain/ (merge, git,
        # recovery logic), not just the three files historically loaded here
        # — that hardcoded allowlist is why L3 escalated ~100% of the time
        # instead of proposing a fix. Load the whole directory, with the
        # highest-signal files guaranteed to load first (in case the bundle
        # budget is exhausted before the glob finishes) and a wider per-file
        # cap since several chain/ files exceed the generic 16KB one.
        chain_dir = factory_dir / "chain"
        # Priority = the dispatch/merge/git/recovery surface where dispatch_code
        # bugs actually live. The small git files (worktree/branch/rollback)
        # are listed explicitly so a budget cutoff can never drop them the way
        # a plain alphabetical glob did (worktree.py sorts after factory_*).
        _priority_names = (
            "orchestrator.py",
            "handlers.py",
            "state_machine.py",
            "auto_merge.py",
            "worktree.py",
            "branch.py",
            "rollback.py",
        )
        _seen: set[Path] = set()
        running_total = 0

        # Priority files always load first, regardless of budget.
        for name in _priority_names:
            p = chain_dir / name
            if not p.exists():
                continue
            _seen.add(p)
            content = _read_file_capped(p, _CHAIN_FILE_CAP)
            files[_rel(p)] = content
            running_total += len(content)

        # Remaining chain/ files fill the rest of the budget in glob order;
        # once exhausted, stop adding (lower-signal files are dropped, not
        # truncated further — the priority files above stay whole).
        if chain_dir.exists():
            for p in sorted(chain_dir.glob("*.py")):
                if p in _seen:
                    continue
                content = _read_file_capped(p, _CHAIN_FILE_CAP)
                if running_total + len(content) > _DISPATCH_CODE_BUNDLE_CAP:
                    break
                files[_rel(p)] = content
                running_total += len(content)

    elif proposed_area == "detector_tool":
        detectors_dir = factory_dir / "manager" / "detectors"
        for p in sorted(detectors_dir.glob("*.py")):
            files[_rel(p)] = _read_file(p)
        signals_py = factory_dir / "manager" / "signals.py"
        if signals_py.exists():
            files[_rel(signals_py)] = _read_file(signals_py)

    elif proposed_area == "observability":
        obs_dir = factory_dir / "observability"
        if obs_dir.exists():
            for p in sorted(obs_dir.glob("*.py")):
                files[_rel(p)] = _read_file(p)
        signals_py = factory_dir / "manager" / "signals.py"
        if signals_py.exists():
            files[_rel(signals_py)] = _read_file(signals_py)

    else:
        # unknown or any other value: provide a file listing + a few key files.
        # chain/handlers.py and chain/auto_merge.py were previously omitted
        # here even though the file listing below names them — an "unknown"
        # concern about dispatch/merge behavior had no chance of a correct L3
        # diagnosis without them.
        for name in (
            "__main__.py",
            "chain/orchestrator.py",
            "chain/handlers.py",
            "chain/auto_merge.py",
            "runner.py",
        ):
            cap = _CHAIN_FILE_CAP if name.startswith("chain/") else _SOURCE_FILE_CAP
            p = factory_dir / name
            if not p.exists():
                # try without the subpath
                p2 = factory_dir / Path(name).name
                if p2.exists():
                    files[_rel(p2)] = _read_file_capped(p2, cap)
                    continue
            if p.exists():
                files[_rel(p)] = _read_file_capped(p, cap)
        # File listing (capped at 100 lines)
        py_files: list[str] = []
        for p in sorted(factory_dir.rglob("*.py")):
            if "__pycache__" in str(p):
                continue
            py_files.append(_rel(p))
            if len(py_files) >= 100:
                break
        md_files: list[str] = []
        for p in sorted(factory_dir.rglob("*.md")):
            if "__pycache__" in str(p):
                continue
            md_files.append(_rel(p))
            if len(md_files) >= 20:
                break
        listing = "Python files under factory/:\n" + "\n".join(py_files)
        if md_files:
            listing += "\n\nMarkdown files under factory/:\n" + "\n".join(md_files)
        files["[factory-file-listing]"] = listing

    # ---------------------------------------------------------------------------
    # Phase 9: augment with factory self-context modules when available.
    # The mapping is deterministic (no LLM); the "intelligence" is the LLM
    # consuming the loaded markdown.
    # ---------------------------------------------------------------------------
    _CONTEXT_MODULE_CAP = 16 * 1024  # 16 KB per module (per spec)

    _AREA_TO_MODULES: dict[str, list[str]] = {
        "prompt": ["personas"],
        "prompt_edit": ["personas"],
        "persona_settings": ["personas"],
        "dispatch_code": ["orchestrator", "state-machine", "dispatch"],
        "detector_tool": ["observability", "manager"],
        "observability": ["observability", "manager"],
    }

    _context_root = root if root is not None else factory_dir.parent
    context_modules_dir = _context_root / "apps" / "factory" / "context" / "modules"
    if proposed_area in _AREA_TO_MODULES:
        module_names = _AREA_TO_MODULES[proposed_area]
    else:
        # unknown / anything else → include all six
        module_names = ["orchestrator", "personas", "state-machine", "observability", "dispatch", "manager"]

    for mod_name in module_names:
        mod_path = context_modules_dir / f"{mod_name}.md"
        if not mod_path.exists():
            continue  # not yet generated — skip silently
        try:
            text = mod_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > _CONTEXT_MODULE_CAP:
            text = text[:_CONTEXT_MODULE_CAP] + "\n...[truncated at 16KB]"
        files[f"[context-module:{mod_name}]"] = text

    # Enforce the bundle cap instead of merely warning. Previously this only
    # logged when the total exceeded the cap, so the frontier model silently
    # received an over-budget bundle (e.g. prompt_edit loads ALL personas →
    # ~147KB) and the provider truncated it arbitrarily — degrading every L3
    # diagnosis. Deterministically shrink each file to an equal share of the
    # budget so the total stays under the cap and every file remains visible
    # (truncated, not dropped). Files already under their share are untouched.
    effective_cap = (
        _BUNDLE_TOTAL_CAP if proposed_area in _NARROW_BUNDLE_AREAS else _WIDE_BUNDLE_TOTAL_CAP
    )
    total = sum(len(v) for v in files.values())
    if total > effective_cap and files:
        per_file_budget = max(1, effective_cap // len(files))
        trimmed = 0
        for key in list(files):
            if len(files[key]) > per_file_budget:
                files[key] = (
                    files[key][:per_file_budget]
                    + f"\n...[truncated to {per_file_budget} chars to fit the "
                    f"{effective_cap}-char L3 context budget across "
                    f"{len(files)} files]"
                )
                trimmed += 1
        new_total = sum(len(v) for v in files.values())
        print(
            f"[diagnostician] bundle was {total} chars (>{effective_cap} cap); "
            f"trimmed {trimmed}/{len(files)} files to {per_file_budget} chars each "
            f"-> {new_total} chars.",
            file=sys.stderr,
        )

    return files


# ---------------------------------------------------------------------------
# Sentinel / fallback
# ---------------------------------------------------------------------------


def _sentinel_proposal(*, concern_title: str, error: str) -> dict[str, Any]:
    """Return a safe escalate_to_human proposal when L3 LLM fails."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "concern_title": concern_title,
        "diagnosis": (
            f"L3 LLM failed to produce a parseable proposal. "
            f"Error: {error}\n\n"
            "This is a meta-failure: the diagnostician itself could not complete its task. "
            "Human review is required to diagnose the original concern."
        ),
        "proposal": {
            "kind": "observability",
            "target": "",
            "rationale": "LLM failure — no patch possible.",
            "suggested_patch": "",
            "verification": "",
            "confidence": "low",
        },
        "target_class": "escalate_to_human",
        "escalate_to_human": True,
        "escalation_reason": f"L3 LLM parse failure: {error}",
    }


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Failed-apply memory
# ---------------------------------------------------------------------------

# Status strings that represent a failed apply attempt.
_FAILED_APPLY_STATUSES = frozenset({
    "test_failed",
    "patch_failed",
    "abandoned",
    "push_failed",
    "pr_failed",
})

# Maximum entries shown in the "Prior failed attempts" section.
_MAX_PRIOR_FAILURES = 5

# Maximum characters of suggested_patch excerpt per entry.
_PATCH_EXCERPT_CAP = 800


def _load_recent_failed_applies(
    *,
    root: Path,
    concern_title: str,
    lookback_hours: int = 24,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read state/.manager_apply_history.json and return records for recent
    failed apply attempts that match *concern_title*.

    Matching: an entry matches when the referenced proposal file contains a
    ``concern_title`` field equal to *concern_title*.  The entry's ``ts`` field
    must be within ``lookback_hours`` of now (UTC).

    For each matching entry, the referenced proposal JSON is loaded and the
    following fields are extracted:
      - ``proposal_path``   (from the history entry)
      - ``ts``              (from the history entry)
      - ``status``          (from the history entry)
      - ``proposal.target`` (from the proposal JSON, or "")
      - ``proposal.kind``   (from the proposal JSON, or "")
      - ``patch_excerpt``   (first _PATCH_EXCERPT_CAP chars of suggested_patch)

    Parameters
    ----------
    now:
        Reference time for the lookback window.  Defaults to
        ``datetime.now(UTC)`` if not provided.  Pass explicitly in tests
        to avoid wall-clock dependency.

    Returns a list ordered newest-first, capped at ``_MAX_PRIOR_FAILURES``.
    Empty list if no matches or if the history file does not exist.
    """
    history_path = root / "state" / ".manager_apply_history.json"
    if not history_path.exists():
        return []

    try:
        raw = json.loads(history_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
    except (OSError, json.JSONDecodeError):
        return []

    now_utc = now if now is not None else datetime.now(UTC)
    cutoff = now_utc.timestamp() - lookback_hours * 3600

    matched: list[dict[str, Any]] = []

    for entry in raw:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status", "")
        if status not in _FAILED_APPLY_STATUSES:
            continue

        # Time filter.
        ts_str = entry.get("ts", "")
        try:
            ts_dt = datetime.fromisoformat(ts_str)
            if ts_dt.timestamp() < cutoff:
                continue
        except (ValueError, TypeError):
            continue

        # Concern-title filter: load the proposal JSON and check.
        proposal_path_str = entry.get("proposal_path", "")
        if not proposal_path_str:
            continue
        proposal_path = Path(proposal_path_str)

        try:
            proposal_doc = json.loads(proposal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Proposal file missing or corrupt — skip.
            continue

        if not isinstance(proposal_doc, dict):
            continue

        if proposal_doc.get("concern_title") != concern_title:
            continue

        # Extract proposal sub-fields.
        inner = proposal_doc.get("proposal", {})
        if not isinstance(inner, dict):
            inner = {}

        suggested_patch = inner.get("suggested_patch", "") or ""
        patch_excerpt = suggested_patch[:_PATCH_EXCERPT_CAP]

        matched.append({
            "proposal_path": proposal_path_str,
            "ts": ts_str,
            "status": status,
            "target": inner.get("target", ""),
            "kind": inner.get("kind", ""),
            "patch_excerpt": patch_excerpt,
        })

    # Sort newest-first by ts string (ISO-8601 lexicographic sort is correct).
    matched.sort(key=lambda e: e.get("ts", ""), reverse=True)

    return matched[:_MAX_PRIOR_FAILURES]


def _render_prior_failures_section(
    failed_applies: list[dict[str, Any]],
    total_in_history: int | None = None,
) -> str:
    """Render the "Prior failed attempts" section for injection into the user message.

    Parameters
    ----------
    failed_applies:
        The (already-capped) list of failed apply records from
        ``_load_recent_failed_applies``.
    total_in_history:
        If the history had more than ``_MAX_PRIOR_FAILURES`` entries, pass
        the total count so the section can mention it.  If None, no mention.
    """
    n = len(failed_applies)
    lines: list[str] = [
        "## Prior failed attempts on this concern (last 24h)",
        "",
        f"L4 has previously tried to apply {n} proposal(s) for this same concern "
        "title; each was rejected by the apply pipeline. "
        "DO NOT re-propose the same approach. The prior attempts:",
        "",
    ]

    for i, rec in enumerate(failed_applies, start=1):
        lines.append(
            f"{i}. status: {rec['status']}   target: {rec['target']}"
        )
        excerpt = rec.get("patch_excerpt", "")
        if excerpt:
            lines.append("   patch excerpt:")
            lines.append("   ```")
            for line in excerpt.splitlines():
                lines.append(f"   {line}")
            lines.append("   ```")
        lines.append("")

    if total_in_history is not None and total_in_history > _MAX_PRIOR_FAILURES:
        lines.append(
            f"_(showing {_MAX_PRIOR_FAILURES} of {total_in_history} total failed "
            "attempts within the lookback window)_"
        )
        lines.append("")

    lines.append(
        "Consider why these attempts failed (schema violations, broken syntax, "
        "test regressions) and propose a DIFFERENT approach. If you can identify "
        "no plausible alternative within your context, set "
        'target_class="escalate_to_human" with a clear reason.'
    )

    return "\n".join(lines)


def _build_user_message(
    *,
    persona_prompt: str,
    concern: dict[str, Any],
    source_files: dict[str, str],
    detector_hint: list[str],
    now: datetime,
    prior_failed_applies: list[dict[str, Any]] | None = None,
) -> str:
    """Assemble the full user message sent to the L3 LLM.

    Order:
    1. Persona prompt
    2. Prior failed attempts (if any) — injected near the top so the LLM
       reads them before the concern document
    3. Context header
    4. Concern document (full JSON)
    5. Pre-loaded source files (clearly delimited)
    6. Detector hint
    7. Instruction to return JSON
    """
    parts: list[str] = [
        persona_prompt.rstrip(),
        "",
    ]

    # Prior failed attempts — injected before the context bundle.
    if prior_failed_applies:
        parts.append(_render_prior_failures_section(prior_failed_applies))
        parts.append("")

    parts += [
        "---",
        "",
        "## Diagnostician context bundle",
        "",
        f"- **now_ts**: {now.isoformat()}",
        f"- **concern_title**: {concern.get('title', '?')}",
        f"- **proposed_area**: {concern.get('proposed_area', 'unknown')}",
        "",
    ]

    # Concern document.
    parts.append("### Concern document (full JSON from L2)")
    parts.append("")
    parts.append("```json")
    parts.append(json.dumps(concern, indent=2, default=str))
    parts.append("```")
    parts.append("")

    # Pre-loaded source files.
    parts.append("### Pre-loaded factory source files")
    parts.append("")
    parts.append(
        "The files below are the **current HEAD contents** of factory source "
        "files relevant to the concern's `proposed_area`. Use the exact line "
        "contents as context lines in your unified diff."
    )
    parts.append("")

    if source_files:
        for rel_path, content in source_files.items():
            parts.append(f"#### `{rel_path}`")
            parts.append("")
            parts.append("```")
            parts.append(content)
            parts.append("```")
            parts.append("")
    else:
        parts.append("_(no source files pre-loaded for this proposed_area)_")
        parts.append("")

    # Detector hint.
    parts.append("### Available detector modules")
    parts.append("")
    parts.append(
        "If your proposal involves adding or modifying a detector tool, "
        "these modules already exist under `factory/manager/detectors/`:"
    )
    parts.append("")
    if detector_hint:
        for name in detector_hint:
            parts.append(f"- `{name}`")
    else:
        parts.append("_(detector listing unavailable)_")
    parts.append("")

    # Final instruction.
    parts.append("---")
    parts.append("")
    parts.append(
        "Return ONLY the JSON object described in the output schema. "
        "No markdown fences, no prose before or after the JSON object."
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM call with retry
# ---------------------------------------------------------------------------


def _call_llm(
    *,
    user_message: str,
    model_id: str,
    max_tokens: int,
    concern_title: str,
) -> dict[str, Any]:
    """Call the L3 LLM and parse JSON.  Retries once on parse failure.

    On two consecutive failures, returns a sentinel proposal without raising.
    """
    # First attempt.
    try:
        result = text_run(
            "manager_diagnostician",
            user_message,
            model_id,
            schema=_DIAGNOSTICIAN_SCHEMA,
            max_tokens=max_tokens,
        )
        if isinstance(result, dict):
            return result
        parsed = json.loads(str(result))
        if isinstance(parsed, dict):
            return parsed
        return _sentinel_proposal(
            concern_title=concern_title,
            error=f"non-dict top-level result: {str(result)[:200]}",
        )
    except json.JSONDecodeError as exc:
        first_error = repr(exc)
    except Exception as exc:  # noqa: BLE001
        first_error = repr(exc)
        return _sentinel_proposal(
            concern_title=concern_title,
            error=f"text_run_failed: {first_error}",
        )

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
            "manager_diagnostician",
            retry_message,
            model_id,
            schema=_DIAGNOSTICIAN_SCHEMA,
            max_tokens=max_tokens,
        )
        if isinstance(result, dict):
            return result
        parsed = json.loads(str(result))
        if isinstance(parsed, dict):
            return parsed
        return _sentinel_proposal(
            concern_title=concern_title,
            error=f"retry non-dict: {str(result)[:200]}",
        )
    except Exception as exc:  # noqa: BLE001
        return _sentinel_proposal(
            concern_title=concern_title,
            error=f"retry_failed: {repr(exc)}",
        )


# ---------------------------------------------------------------------------
# Find unprocessed concern
# ---------------------------------------------------------------------------


def _proposal_id(concern_id: str, target_area: str) -> str:
    """Return a stable ``proposal_id`` derived from the source concern.

    The id hashes the canonical ``concern_id`` plus the concern's stable
    ``proposed_area``. Because both inputs are content-derived (not the
    volatile ts-slug filename), re-diagnosing the SAME concern yields the SAME
    proposal_id — which is what lets L4 recognise a re-emitted proposal even
    when it lands under a fresh timestamped path.
    """
    material = f"{concern_id}|{target_area}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _is_concern_processed(root: Path, concern: dict[str, Any]) -> bool:
    """Return True if a proposal already exists in state/manager_proposals/ for this concern.

    Matching is by the canonical **concern_id** (the WS0.1 content signature),
    so a re-fired concern is recognised as already-diagnosed regardless of any
    LLM retitle, and two genuinely-different concerns that happen to share a
    title are NOT collapsed. Legacy proposals written before concern_id existed
    fall back to the old ``concern_title`` match so nothing already-processed
    re-fires.
    """
    proposals_dir = _proposals_dir(root)
    if not proposals_dir.exists():
        return False
    from factory.manager.summarizer import concern_id_for

    target_id = concern_id_for(concern)
    concern_title = concern.get("title", "")
    for p in proposals_dir.glob("*.json"):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        doc_id = doc.get("concern_id")
        if isinstance(doc_id, str) and doc_id:
            # Modern proposal: match on stable content id only.
            if doc_id == target_id:
                return True
        elif doc.get("concern_title") == concern_title:
            # Legacy proposal (no concern_id): fall back to title match.
            return True
    return False


def _find_unprocessed_concern(root: Path) -> tuple[Path, dict[str, Any]] | None:
    """Find the most-recent unprocessed concern in state/concerns/.

    "Unprocessed" means no matching entry in ``state/manager_proposals/``.
    Returns ``(path, concern_dict)`` or ``None`` if nothing found.
    """
    concerns_dir = _concerns_dir(root)
    if not concerns_dir.exists():
        return None

    files = sorted(concerns_dir.glob("*.json"), reverse=True)  # newest first
    for f in files:
        try:
            concern = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if concern.get("schema_version") != 1:
            continue
        if not _is_concern_processed(root, concern):
            return f, concern
    return None


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------


def _sanitize_slug(title: str) -> str:
    """Convert a title to a safe filename slug."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9\-]+", "-", slug)
    slug = slug.strip("-")
    slug = slug[:60].rstrip("-")
    return slug or "unnamed-proposal"


def _write_proposal(
    root: Path,
    proposal: dict[str, Any],
    now: datetime,
) -> Path:
    """Write proposal to state/manager_proposals/<ts>-<slug>.json.

    Also appends a compact entry to state/events/proposals.ndjson.
    Returns the path written.
    """
    proposals_dir = _proposals_dir(root)
    concern_title = proposal.get("concern_title", "unnamed")
    slug = _sanitize_slug(concern_title)
    ts_prefix = now.strftime("%Y%m%dT%H%M%S")
    filename = f"{ts_prefix}-{slug}.json"

    try:
        proposals_dir.mkdir(parents=True, exist_ok=True)
        proposal_path = proposals_dir / filename
        proposal_doc = {"schema_version": _SCHEMA_VERSION, **proposal}
        proposal_path.write_text(
            json.dumps(proposal_doc, indent=2, default=str), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[diagnostician] failed to write proposal file: {exc}", file=sys.stderr)
        proposal_path = proposals_dir / filename

    # Append compact event to proposals.ndjson.
    event_path = _proposals_event_path(root)
    event: dict[str, Any] = {
        "ts": now.isoformat(),
        "schema_version": _SCHEMA_VERSION,
        "event": "proposal_emitted",
        "concern_title": proposal.get("concern_title", ""),
        "concern_id": proposal.get("concern_id", ""),
        "proposal_id": proposal.get("proposal_id", ""),
        "target_class": proposal.get("target_class", ""),
        "escalate_to_human": proposal.get("escalate_to_human", False),
        "proposal_path": str(proposal_path),
    }
    try:
        event_path.parent.mkdir(parents=True, exist_ok=True)
        with event_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[diagnostician] failed to append proposal event: {exc}", file=sys.stderr)

    return proposal_path


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------


def run_diagnostician_once(
    *,
    root: Path,
    concern_path: Path | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """Run one diagnostician cycle.

    1. If ``concern_path`` is None, find the most-recent unprocessed concern
       in ``state/concerns/*.json``.  "Unprocessed" means no entry in
       ``state/manager_proposals/`` matching its title.  If nothing unprocessed,
       return None.
    2. Load the concern.  Verify ``schema_version=1``.
    3. Pre-load factory source files relevant to ``concern.proposed_area``.
    4. Build the user message.
    5. Call the LLM (skipped in dry-run mode).
    6. On JSON parse failure: retry once; on second failure return a sentinel
       proposal with ``target_class="escalate_to_human"``.
    7. Write the proposal to ``state/manager_proposals/<ts>-<title>.json``
       and append a compact entry to ``state/events/proposals.ndjson``.
    8. Return the parsed proposal dict with a ``proposal_path`` field added.

    Parameters
    ----------
    root:
        Factory root directory.
    concern_path:
        If provided, diagnose this specific concern file.  If None, find the
        most-recent unprocessed concern automatically.
    now:
        Override the current time (useful for tests).
    dry_run:
        If True, assemble the prompt but do NOT call the LLM.  Prints the
        user message to stdout and returns a sentinel proposal.

    Returns
    -------
    dict | None
        The proposal dict (plus ``proposal_path`` field) if a proposal was
        produced, or ``None`` if there were no unprocessed concerns.
    """
    from factory.model_router import max_output_tokens_for, route

    root = Path(root)
    now = now or datetime.now(UTC)

    # Step 1: find or validate the concern.
    if concern_path is not None:
        concern_path = Path(concern_path)
        try:
            concern = json.loads(concern_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"[diagnostician] failed to load concern from {concern_path}: {exc}",
                file=sys.stderr,
            )
            return None
        if concern.get("schema_version") != 1:
            print(
                f"[diagnostician] concern at {concern_path} has unsupported "
                f"schema_version={concern.get('schema_version')}; expected 1.",
                file=sys.stderr,
            )
            return None
    else:
        found = _find_unprocessed_concern(root)
        if found is None:
            return None
        concern_path, concern = found

    # Step 2: determine factory source dir.
    # factory_dir is the parent of this file: factory/manager/diagnostician.py
    # → factory/ is two levels up from here.
    factory_dir = Path(__file__).resolve().parent.parent

    # Step 3: pre-load source files.
    proposed_area = concern.get("proposed_area", "unknown")
    source_files = _pre_load_source(proposed_area, factory_dir=factory_dir, root=root)

    # Step 4: build detector hint.
    detectors_dir = factory_dir / "manager" / "detectors"
    detector_hint: list[str] = []
    if detectors_dir.exists():
        for p in sorted(detectors_dir.glob("*.py")):
            if p.name.startswith("_"):
                continue
            detector_hint.append(p.name)

    # Step 5: load persona prompt and build user message.
    persona_prompt = _read_persona_prompt("manager_diagnostician")

    # Load prior failed apply attempts for this concern title (failed-apply memory).
    concern_title = concern.get("title", "unnamed")
    prior_failed_applies = _load_recent_failed_applies(
        root=root,
        concern_title=concern_title,
        now=now,
    )

    user_message = _build_user_message(
        persona_prompt=persona_prompt,
        concern=concern,
        source_files=source_files,
        detector_hint=detector_hint,
        now=now,
        prior_failed_applies=prior_failed_applies if prior_failed_applies else None,
    )

    if dry_run:
        print(user_message)
        proposal: dict[str, Any] = {
            "concern_title": concern_title,
            "diagnosis": "<dry-run — LLM not called>",
            "proposal": {
                "kind": "observability",
                "target": "",
                "rationale": "<dry-run>",
                "suggested_patch": "",
                "verification": "",
                "confidence": "low",
            },
            "target_class": "escalate_to_human",
            "escalate_to_human": True,
            "escalation_reason": "dry-run mode — LLM not called",
        }
    else:
        # Step 6: call LLM with retry.
        model_id = route("manager_diagnostician")
        max_tokens = max_output_tokens_for(model_id)
        proposal = _call_llm(
            user_message=user_message,
            model_id=model_id,
            max_tokens=max_tokens,
            concern_title=concern_title,
        )

    # Stamp the canonical stable ids so L4 dedup (and any re-diagnosis of the
    # same concern) keys off content, not the ts-slug filename. Derived from the
    # source concern, so a re-fired concern yields the SAME proposal_id.
    from factory.manager.summarizer import concern_id_for

    cid = concern_id_for(concern)
    proposal["concern_id"] = cid
    proposal["proposal_id"] = _proposal_id(cid, concern.get("proposed_area", ""))

    # Step 7: write proposal.
    proposal_path = _write_proposal(root, proposal, now)
    proposal["proposal_path"] = str(proposal_path)

    # Step 8 (Phase 7): handle halt request.
    # Only L3 (this module) can set the halt state.  L1 and L2 have no
    # halt authority — they never touch factory.manager.halt.
    halt_requested = False
    if not dry_run and proposal.get("request_halt") is True:
        halt_reason = proposal.get("halt_reason")
        if halt_reason and isinstance(halt_reason, str) and halt_reason.strip():
            try:
                from factory.manager.halt import request_halt as _request_halt

                halt_file = _request_halt(
                    root=root,
                    concern_title=concern_title,
                    proposal_path=str(proposal_path),
                    reason=halt_reason.strip(),
                )
                if halt_file is None:
                    # Operator cleared a halt moments ago — the resume grace
                    # window overrides halt authority so the factory can
                    # demonstrate liveness instead of deadlocking against
                    # its own manager. The proposal still lands for review.
                    proposal["halt_suppressed_by_resume_grace"] = True
                    print(
                        f"[diagnostician] halt SUPPRESSED (operator resume "
                        f"grace window): concern={concern_title!r}",
                        file=sys.stderr,
                    )
                else:
                    halt_requested = True
                    print(
                        f"[diagnostician] HALT requested: concern={concern_title!r} "
                        f"reason={halt_reason!r}",
                        file=sys.stderr,
                    )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[diagnostician] WARNING: halt request failed: {exc!r}",
                    file=sys.stderr,
                )
        else:
            # request_halt=true but no valid halt_reason — silently drop.
            print(
                "[diagnostician] WARNING: request_halt=true but halt_reason is "
                "null or empty — halt NOT triggered.",
                file=sys.stderr,
            )

    # Record whether this proposal triggered a halt in the proposal output.
    proposal["halt_requested"] = halt_requested

    # Rewrite the proposal file with the halt_requested annotation.
    try:
        proposal_doc = {"schema_version": _SCHEMA_VERSION, **proposal}
        proposal_path.write_text(
            json.dumps(proposal_doc, indent=2, default=str), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[diagnostician] WARNING: failed to re-write proposal with halt annotation: {exc}",
            file=sys.stderr,
        )

    return proposal


def run_diagnostician_daemon(
    *,
    root: Path,
    interval_s: int = 300,
    max_iters: int | None = None,
) -> None:
    """Loop ``run_diagnostician_once`` every ``interval_s`` seconds.

    Runs until interrupted by SIGINT (KeyboardInterrupt) or until
    ``max_iters`` iterations have completed (when provided — useful
    for tests).

    Default cadence is 5 minutes (300s) — L3 is expensive; it should
    only run when there are unprocessed concerns.

    Parameters
    ----------
    root:
        Factory root directory.
    interval_s:
        Seconds to sleep between diagnostician runs.  Default 300 (5 min).
    max_iters:
        If set, exit after this many iterations.  If None, run forever.
    """
    iterations = 0
    print(
        f"[diagnostician] starting daemon (interval_s={interval_s})",
        file=sys.stderr,
    )
    try:
        while True:
            # Phase 8 (Phase 7 reviewer note): check halt state before each
            # iteration so the daemon skips LLM work while halted.
            # Circuit-breaker: log if tripped but keep running.
            try:
                from factory.manager.halt import is_halted as _is_halted
                if _is_halted(root=root):
                    print(
                        "[diagnostician] factory halted: skipping iteration",
                        file=sys.stderr,
                    )
                    iterations += 1
                    if max_iters is not None and iterations >= max_iters:
                        print(
                            f"[diagnostician] reached max_iters={max_iters}, stopping.",
                            file=sys.stderr,
                        )
                        break
                    time.sleep(interval_s)
                    continue
            except Exception as _halt_exc:  # noqa: BLE001
                # Corrupt halt FILEs fail safe inside is_halted; this only fires
                # on a broken halt MODULE. Fail-open but make it a visible alert.
                try:
                    from factory.manager.signals import write_alert_event

                    write_alert_event(
                        "halt_check_module_error",
                        f"[diagnostician] halt-check raised {_halt_exc!r}; continuing (fail-open)",
                        severity="critical",
                        software_factory_root=root,
                    )
                except Exception:  # noqa: BLE001 - alerting is best-effort
                    print(
                        f"[diagnostician] CRITICAL: halt-check failed: {_halt_exc!r}; continuing (fail-open)",
                        file=sys.stderr,
                    )

            try:
                from factory.manager.circuit_breaker import is_tripped as _cb_is_tripped
                if _cb_is_tripped(root=root):
                    print(
                        "[diagnostician] NOTE: circuit breaker is tripped; L4 apply is halted. "
                        "Detection and proposals continue.",
                        file=sys.stderr,
                    )
            except Exception:  # noqa: BLE001
                pass

            try:
                result = run_diagnostician_once(root=root)
                if result is None:
                    print(
                        "[diagnostician] no unprocessed concerns, skipping.",
                        file=sys.stderr,
                    )
                else:
                    title = result.get("concern_title", "?")
                    target_class = result.get("target_class", "?")
                    esc = result.get("escalate_to_human", False)
                    esc_tag = " [ESCALATE→HUMAN]" if esc else ""
                    print(
                        f"[diagnostician] concern={title!r} "
                        f"target_class={target_class}{esc_tag}",
                        file=sys.stderr,
                    )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[diagnostician] run_diagnostician_once raised: {exc!r}",
                    file=sys.stderr,
                )

            iterations += 1
            if max_iters is not None and iterations >= max_iters:
                print(
                    f"[diagnostician] reached max_iters={max_iters}, stopping.",
                    file=sys.stderr,
                )
                break

            time.sleep(interval_s)
    except KeyboardInterrupt:
        print("\n[diagnostician] interrupted, shutting down.", file=sys.stderr)


__all__ = [
    "run_diagnostician_once",
    "run_diagnostician_daemon",
    "_pre_load_source",
    "_load_recent_failed_applies",
    "_render_prior_failures_section",
]
