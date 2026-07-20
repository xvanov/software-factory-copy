"""Lightweight EARS parsing for property-based acceptance oracles (WS4.3).

EARS (Easy Approach to Requirements Syntax) is the constrained-natural-language
form the story/direction templates ask for acceptance criteria to be written in
(see ``factory/artifacts/story_template.md`` and ``factory/personas/sm.md``):

    AC<n>.<m>: WHEN <trigger>, [GIVEN <precondition>,] THE <system> SHALL <response>

The defining keyword is ``SHALL`` — it separates the *condition* (the situation
the requirement applies in) from the *response* (the observable behaviour the
system must exhibit, i.e. the INVARIANT). Because an EARS ``SHALL`` names an
invariant rather than one example, it maps cleanly onto a property-based test:
the response becomes a property asserted over MANY generated inputs, not a
single hand-picked example.

This module is deliberately a heuristic, not a grammar. It:

* detects whether an acceptance-criterion line is EARS-shaped (``is_ears``);
* decomposes an EARS line into structured parts (``parse_ears`` →
  :class:`EarsClause`) so the acceptance author gets an explicit
  trigger / precondition / system / response instead of prose to re-parse; and
* falls back safely — anything it cannot confidently split returns ``None`` and
  the caller keeps the existing example-based acceptance flow.

Nothing here calls an LLM or touches disk; it is pure text and safe to import
anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ``SHALL`` is the EARS discriminator: its presence is what turns a bullet into a
# requirement with an invariant we can assert as a property. A line with no
# ``SHALL`` is treated as ordinary prose and left to example-mode.
_SHALL = re.compile(r"\bSHALL\b", re.IGNORECASE)

# Optional ``AC1.2:`` / ``AC3 -`` style trace prefix.
_AC_PREFIX = re.compile(r"^\s*(AC\s*\d+(?:\.\d+)?)\s*[:.\-)]\s*", re.IGNORECASE)

# Leading EARS trigger keywords and the requirement "kind" each denotes.
_TRIGGER = re.compile(r"^(WHEN|WHILE|IF|WHERE)\b\s*(.*)$", re.IGNORECASE | re.DOTALL)
_TRIGGER_KIND = {
    "WHEN": "event",
    "WHILE": "state",
    "IF": "unwanted",
    "WHERE": "optional",
}

_GIVEN = re.compile(r"\bGIVEN\b\s*(.*)$", re.IGNORECASE | re.DOTALL)
_THE = re.compile(r"\bTHE\b", re.IGNORECASE)
# ``IF <cond>, THEN THE system SHALL`` — strip the dangling connective.
_TRAILING_THEN = re.compile(r",?\s*THEN\s*$", re.IGNORECASE)

# Explicit "this AC can't be tested" marker the SM/author emit for vague ACs
# (see sm.md). Never treat it as a property — it has no invariant.
_UNTESTABLE = "UNTESTABLE"


@dataclass(frozen=True)
class EarsClause:
    """A parsed EARS acceptance criterion.

    ``response`` is the invariant (the ``SHALL`` clause) — the thing a property
    test asserts over generated inputs. ``trigger`` / ``precondition`` describe
    the situation the invariant holds in and guide input generation. ``kind`` is
    the EARS category (event / state / unwanted / optional / ubiquitous).
    """

    raw: str
    kind: str
    response: str
    trigger: str | None = None
    precondition: str | None = None
    system: str | None = None
    ac_id: str | None = None


def _clean(text: str) -> str:
    return text.strip().strip(",").strip()


def parse_ears(text: str) -> EarsClause | None:
    """Parse one acceptance-criterion line into an :class:`EarsClause`.

    Returns ``None`` when the line is not EARS-shaped (no ``SHALL``), is an
    explicit UNTESTABLE marker, or is too malformed to yield a response — in all
    of those cases the caller falls back to example-mode. Never raises.
    """
    if not text or not text.strip():
        return None
    s = text.strip()

    ac_id: str | None = None
    m = _AC_PREFIX.match(s)
    if m:
        ac_id = re.sub(r"\s+", "", m.group(1)).upper()
        s = s[m.end():]

    if _UNTESTABLE in s.upper():
        return None

    shall = _SHALL.search(s)
    if shall is None:
        return None

    response = _clean(s[shall.end():])
    if not response:
        # ``... SHALL`` with nothing after it is malformed → example-mode.
        return None

    left = _clean(s[: shall.start()])

    # Peel "THE <system>" off the end of the condition, if present. Use the LAST
    # "THE" so a lowercase "the" earlier in the trigger ("WHEN the user ...")
    # is not mistaken for the system actor.
    system: str | None = None
    condition = left
    the_hits = list(_THE.finditer(left))
    if the_hits:
        last = the_hits[-1]
        candidate = _clean(left[last.end():])
        if candidate:
            system = candidate
            condition = _clean(left[: last.start()])

    kind = "ubiquitous"
    trigger: str | None = None
    precondition: str | None = None
    tm = _TRIGGER.match(condition)
    if tm:
        kind = _TRIGGER_KIND[tm.group(1).upper()]
        rest = _clean(_TRAILING_THEN.sub("", tm.group(2)))
        gm = _GIVEN.search(rest)
        if gm:
            precondition = _clean(gm.group(1))
            rest = _clean(rest[: gm.start()])
        trigger = rest or None

    return EarsClause(
        raw=text.strip(),
        kind=kind,
        response=response,
        trigger=trigger,
        precondition=precondition,
        system=system,
        ac_id=ac_id,
    )


def is_ears(text: str) -> bool:
    """True iff ``text`` is an EARS-shaped acceptance criterion."""
    return parse_ears(text) is not None


def split_acs(acs: list[str]) -> list[tuple[str, EarsClause | None]]:
    """Pair each raw AC with its parsed clause (``None`` when not EARS-shaped)."""
    return [(ac, parse_ears(ac)) for ac in acs]


def ears_clauses(acs: list[str]) -> list[EarsClause]:
    """Just the EARS-parseable clauses among ``acs`` (order preserved)."""
    return [c for _, c in split_acs(acs) if c is not None]


def has_ears(acs: list[str]) -> bool:
    """True iff at least one AC in ``acs`` is EARS-shaped."""
    return any(parse_ears(ac) is not None for ac in acs)


__all__ = [
    "EarsClause",
    "ears_clauses",
    "has_ears",
    "is_ears",
    "parse_ears",
    "split_acs",
]
