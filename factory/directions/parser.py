"""Direction directory → ``Direction`` record.

The directory layout (canonical) is::

    apps/<app>/directions/<id>-<slug>/
      direction.md            # required; YAML frontmatter + body
      flow.md                 # optional; user flow
      api_spec.md             # optional; API contract
      artifacts/              # optional; binaries/mockups
      state.yaml              # auto-managed (status, audit trail)

This module is the read-side: parse a directory into a ``Direction`` record
the rest of the factory can pass around. The write-side is ``creator.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter
import yaml

_SLUG_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_ID_PREFIX_RE = re.compile(r"^(\d{3,})-(.+)$")


@dataclass
class Direction:
    """Parsed direction record. All fields populated from disk."""

    id: str
    slug: str
    title: str
    type_tag: str | None
    why: str | None
    has_flow: bool
    has_api_spec: bool
    acceptance: list[str]
    explore_tag: bool
    artifacts_paths: list[Path]
    app: str
    status: str
    raw_frontmatter: dict[str, Any]
    raw_body: str
    dir_path: Path = field(default_factory=lambda: Path("."))
    state: dict[str, Any] = field(default_factory=dict)


def _parse_acceptance(body: str) -> list[str]:
    """Extract bullets under an ``## Acceptance Criteria`` heading.

    Accepts ``- [ ]`` / ``- [x]`` / plain ``- `` bullets. Stops at the next
    heading line.
    """
    out: list[str] = []
    in_section = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if in_section:
                break
            heading_text = stripped.lstrip("#").strip().lower()
            if heading_text.startswith("acceptance criteria") or heading_text == "acceptance":
                in_section = True
            continue
        if not in_section:
            continue
        m = re.match(r"^[-*]\s*(?:\[[ xX]\]\s*)?(.+)$", stripped)
        if m:
            text = m.group(1).strip()
            if text:
                out.append(text)
    return out


def _parse_why(body: str) -> str | None:
    """Extract the prose under an ``## Why`` heading (case-insensitive)."""
    in_section = False
    collected: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if in_section:
                break
            heading_text = stripped.lstrip("#").strip().lower()
            if heading_text == "why":
                in_section = True
            continue
        if in_section:
            if stripped.startswith("<!--"):
                continue
            collected.append(line)
    text = "\n".join(collected).strip()
    return text or None


def parse_direction_dir(app: str, dir_path: Path) -> Direction:
    """Read a direction directory from disk and return a ``Direction`` record.

    Robust to missing optional files. Raises ``FileNotFoundError`` if
    ``direction.md`` itself is missing.
    """
    dir_path = Path(dir_path)
    direction_md = dir_path / "direction.md"
    if not direction_md.exists():
        raise FileNotFoundError(f"direction.md missing in {dir_path}")

    # ID + slug from the directory name (e.g. "001-add-healthz-endpoint")
    name = dir_path.name
    m = _ID_PREFIX_RE.match(name)
    if m:
        id_ = m.group(1)
        slug = m.group(2)
    else:
        # Tolerate ungrouped names — id becomes ""
        id_ = ""
        slug = name

    post = frontmatter.load(str(direction_md))
    raw_frontmatter: dict[str, Any] = dict(post.metadata or {})
    raw_body = post.content or ""

    title = str(raw_frontmatter.get("title") or "").strip()
    if not title:
        # Fall back to the first ``# heading`` line in the body
        for line in raw_body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
    if not title:
        title = slug.replace("-", " ").title()

    type_tag_raw = raw_frontmatter.get("type")
    type_tag = str(type_tag_raw).strip() if type_tag_raw not in (None, "") else None

    explore_tag = bool(raw_frontmatter.get("explore"))

    flow_path = dir_path / "flow.md"
    has_flow = flow_path.exists() and flow_path.stat().st_size > 0

    api_spec_path = dir_path / "api_spec.md"
    has_api_spec = api_spec_path.exists() and api_spec_path.stat().st_size > 0

    artifacts_dir = dir_path / "artifacts"
    artifacts_paths: list[Path] = []
    if artifacts_dir.is_dir():
        artifacts_paths = sorted(p for p in artifacts_dir.iterdir() if p.is_file())

    state_path = dir_path / "state.yaml"
    state: dict[str, Any] = {}
    status = "created"
    if state_path.exists():
        try:
            loaded = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                state = loaded
                if "status" in state and isinstance(state["status"], str):
                    status = state["status"]
        except yaml.YAMLError:
            # Corrupt state.yaml: keep defaults so the watcher can re-process
            state = {}

    return Direction(
        id=id_,
        slug=slug,
        title=title,
        type_tag=type_tag,
        why=_parse_why(raw_body),
        has_flow=has_flow,
        has_api_spec=has_api_spec,
        acceptance=_parse_acceptance(raw_body),
        explore_tag=explore_tag,
        artifacts_paths=artifacts_paths,
        app=app,
        status=status,
        raw_frontmatter=raw_frontmatter,
        raw_body=raw_body,
        dir_path=dir_path,
        state=state,
    )


def next_direction_id(app: str, software_factory_root: Path) -> str:
    """Return the next zero-padded numeric id for a new direction.

    Scans ``<root>/apps/<app>/directions/`` for the highest ``NNN-`` prefix
    and returns ``NNN+1`` zero-padded to width 3 (or wider, if existing ids
    already exceed 3 digits).
    """
    directions_dir = Path(software_factory_root) / "apps" / app / "directions"
    if not directions_dir.exists():
        return "001"
    max_id = 0
    width = 3
    for entry in directions_dir.iterdir():
        if not entry.is_dir():
            continue
        m = _ID_PREFIX_RE.match(entry.name)
        if not m:
            continue
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if n > max_id:
            max_id = n
        if len(m.group(1)) > width:
            width = len(m.group(1))
    return str(max_id + 1).zfill(width)


def list_direction_dirs(app: str, software_factory_root: Path) -> list[Path]:
    """Return all direction directories for ``app``, sorted by id."""
    directions_dir = Path(software_factory_root) / "apps" / app / "directions"
    if not directions_dir.exists():
        return []
    entries = [
        p
        for p in directions_dir.iterdir()
        if p.is_dir() and (p / "direction.md").exists() and _ID_PREFIX_RE.match(p.name)
    ]
    entries.sort(key=lambda p: p.name)
    return entries


def slugify(text: str) -> str:
    """Lowercase, hyphenate, drop non-alphanumerics. Caps at 60 chars."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", text.strip().lower()).strip("-")
    if not s:
        s = "untitled"
    return s[:60].strip("-") or "untitled"


def validate_slug(slug: str) -> bool:
    """Slug must match ``[A-Za-z0-9_\\-]+``."""
    return bool(_SLUG_RE.match(slug))
