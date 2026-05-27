"""factory.manager.halt — Halt authority for the factory (Phase 7).

Only the L3 Diagnostician may *request* a halt (via ``request_halt``).
Only a human operator may *clear* a halt (via ``clear_halt``).
The driver loop and ``tick()`` each call ``is_halted`` before dispatching.

OPERATOR-ONLY MODULE
--------------------
This file MUST NOT be invoked by any LLM pathway.  It is in the
``factory/manager/*.py`` forbidden class and will never be auto-applied
by the L4 pipeline.  ``clear_halt`` in particular must only be called by
``factory resume`` (a CLI command requiring human interaction).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path  # noqa: E402

# Schema version for the halt state file.
_SCHEMA_VERSION = 1

# Standard filename for the halt mode state.
_HALT_FILE = "factory_mode.json"

# Standard filename for the halt history archive.
_HALT_HISTORY_FILE = ".halt_history.json"


def _halt_path(root: Path) -> Path:
    return root / "state" / _HALT_FILE


def _history_path(root: Path) -> Path:
    return root / "state" / _HALT_HISTORY_FILE


def request_halt(
    *,
    root: Path,
    concern_title: str,
    proposal_path: str | None,
    reason: str,
) -> Path:
    """Write the halt mode file.

    Idempotent — if halt is already set, append the old state to the
    history archive and overwrite with the new halt (most recent wins).

    Returns the path to the halt state file.

    Parameters
    ----------
    root:
        Factory root directory.
    concern_title:
        The L3 concern title that triggered this halt request.
    proposal_path:
        Absolute path (as str) to the proposal file that carried the
        halt request.  May be None when called from tests or tooling.
    reason:
        Free-text justification from the L3 proposal's ``halt_reason``
        field.
    """
    root = Path(root)
    halt_path = _halt_path(root)

    # If halt already set, archive the previous state before overwriting.
    if halt_path.exists():
        try:
            old_state = json.loads(halt_path.read_text(encoding="utf-8"))
            _append_history(root, old_state)
        except (OSError, json.JSONDecodeError):
            pass

    state: dict = {
        "schema_version": _SCHEMA_VERSION,
        "mode": "halted",
        "set_at": datetime.now(UTC).isoformat(),
        "set_by": "manager_diagnostician",
        "concern_title": concern_title,
        "proposal_path": proposal_path,
        "reason": reason,
    }

    halt_path.parent.mkdir(parents=True, exist_ok=True)
    halt_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return halt_path


def is_halted(*, root: Path) -> bool:
    """Return True if the halt mode file exists and mode == "halted"."""
    root = Path(root)
    p = _halt_path(root)
    if not p.exists():
        return False
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
        return state.get("mode") == "halted"
    except (OSError, json.JSONDecodeError):
        return False


def get_halt_state(*, root: Path) -> dict | None:
    """Return the halt state dict or None if not halted."""
    root = Path(root)
    p = _halt_path(root)
    if not p.exists():
        return None
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
        if state.get("mode") == "halted":
            return state
        return None
    except (OSError, json.JSONDecodeError):
        return None


def clear_halt(
    *,
    root: Path,
    cleared_by: str = "operator",
    reason: str | None = None,
) -> dict:
    """Clear the halt mode.

    Moves the current halt state to ``state/.halt_history.json`` and
    removes (or resets) ``state/factory_mode.json``.

    OPERATOR-ONLY — must never be called by any LLM path.

    Parameters
    ----------
    root:
        Factory root directory.
    cleared_by:
        Who cleared the halt.  Defaults to "operator".
    reason:
        Optional free-text reason for clearing.

    Returns
    -------
    dict
        The archived halt state (with ``cleared_at``, ``cleared_by``,
        ``clear_reason`` added).

    Raises
    ------
    FileNotFoundError
        If there is no active halt to clear.
    """
    root = Path(root)
    halt_path = _halt_path(root)

    if not halt_path.exists():
        raise FileNotFoundError(
            f"No halt state file found at {halt_path}; nothing to clear."
        )

    state = json.loads(halt_path.read_text(encoding="utf-8"))
    if state.get("mode") != "halted":
        raise ValueError(
            f"factory_mode.json exists but mode={state.get('mode')!r} (not 'halted'); "
            "nothing to clear."
        )

    # Annotate the archived entry with clearance metadata.
    archived = dict(state)
    archived["cleared_at"] = datetime.now(UTC).isoformat()
    archived["cleared_by"] = cleared_by
    if reason is not None:
        archived["clear_reason"] = reason

    _append_history(root, archived)

    # Remove the halt file so is_halted() returns False.
    halt_path.unlink()

    return archived


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _append_history(root: Path, entry: dict) -> None:
    """Append ``entry`` to the halt history archive."""
    history_path = _history_path(root)
    history: list[dict] = []
    if history_path.exists():
        try:
            data = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                history = data
        except (OSError, json.JSONDecodeError):
            pass
    history.append(entry)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(json.dumps(history, indent=2), encoding="utf-8")


__all__ = [
    "request_halt",
    "is_halted",
    "get_halt_state",
    "clear_halt",
]
