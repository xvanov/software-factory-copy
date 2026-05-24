"""Typer-based `factory` CLI.

Phase-0 subcommands:
  * ``factory --version``
  * ``factory hello``
  * ``factory test-persona dev --story <path> --repo <path> [...]``

Phase-1 additions:
  * ``factory new-direction --app <app>``
  * ``factory tell --app <app> "<text>"``
  * ``factory edit-direction --app <app> <id-or-slug>``
  * ``factory pm-sync --app <app> [--dry-run]``
  * ``factory ingest-issue --app <app> <issue-number>``
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from factory import __phase__, __version__
from factory.model_router import route
from factory.runner import LLMConfig, sandbox_run

app = typer.Typer(help="Factory CLI — orchestrate the software factory.")
test_persona_app = typer.Typer(help="Run a single persona end-to-end for testing.")
app.add_typer(test_persona_app, name="test-persona")

console = Console()

_FACTORY_ROOT = Path(__file__).resolve().parent.parent


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"{__version__} ({__phase__})")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version"
    ),
) -> None:
    """Factory orchestrator."""


@app.command()
def hello() -> None:
    """Sanity check — confirms the CLI is wired up."""
    console.print(
        Panel.fit(
            f"[bold green]factory[/bold green] v{__version__} ({__phase__}) is alive.\n"
            f"Phase-1 commands: [bold]new-direction[/bold], [bold]tell[/bold], "
            f"[bold]edit-direction[/bold], [bold]pm-sync[/bold], [bold]ingest-issue[/bold].",
            title="hello",
        )
    )


@test_persona_app.command("dev")
def test_persona_dev(
    story: Path = typer.Option(..., "--story", exists=True, help="Path to story markdown file"),
    repo: Path = typer.Option(
        ..., "--repo", exists=True, file_okay=False, help="Path to target app repo"
    ),
    difficulty: str = typer.Option(
        "standard", "--difficulty", help="dev difficulty: standard | hard"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Do not call any LLM; assemble prompt and write a stub DB row."
    ),
    task_scope: str | None = typer.Option(
        None, "--task-scope", help="Optional task-scope hint for navigation.md lookup"
    ),
) -> None:
    """Run the Dev persona once against a story + repo."""
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)

    model = route("dev", difficulty=difficulty)
    cfg = LLMConfig(model=model)
    mode_label = "[yellow]DRY-RUN[/yellow]" if dry_run else "[green]REAL RUN[/green]"
    console.print(
        Panel.fit(
            f"persona=[bold]dev[/bold]  difficulty=[bold]{difficulty}[/bold]\n"
            f"model=[bold]{model}[/bold]\n"
            f"story=[bold]{story}[/bold]\n"
            f"repo=[bold]{repo}[/bold]\n"
            f"mode={mode_label}",
            title="factory test-persona dev",
        )
    )
    if not dry_run and not any(
        os.environ.get(k) for k in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    ):
        console.print(
            "[yellow]warning:[/yellow] no provider API keys set; the SDK call will fail. "
            "Pass [bold]--dry-run[/bold] to test wiring."
        )

    result = asyncio.run(
        sandbox_run(
            persona="dev",
            story_path=story,
            repo_path=repo,
            llm_config=cfg,
            difficulty=difficulty,
            dry_run=dry_run,
            task_scope=task_scope,
        )
    )

    color = "green" if result.success else ("yellow" if dry_run else "red")
    console.print(
        Panel(
            f"success={result.success}\n"
            f"test_run_passed={result.test_run_passed}\n"
            f"files_changed={result.files_changed}\n"
            f"tokens_in={result.tokens_in} tokens_out={result.tokens_out} "
            f"cost_usd=${result.cost_usd:.4f}\n"
            f"error={result.error}",
            title="run result",
            style=color,
        )
    )
    if result.summary:
        console.print(Panel(result.summary, title="summary"))

    raise typer.Exit(code=0 if (result.success or dry_run) else 1)


# --------------------------------------------------------------------------- #
# Phase-1 commands
# --------------------------------------------------------------------------- #


def _ensure_github_client() -> Any:
    """Construct a ``pygithub.Github`` client. Fails with a clear message if no token."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        console.print(
            "[red]error:[/red] no GitHub token set. Set [bold]GITHUB_TOKEN[/bold] "
            "or [bold]GH_TOKEN[/bold] in the environment (or .env)."
        )
        raise typer.Exit(code=2)
    from github import Github

    return Github(token)


@app.command("new-direction")
def new_direction(
    app_name: str = typer.Option(..., "--app", help="App name (e.g. sacrifice)"),
) -> None:
    """Interactive direction creation. Walks the user through the prompts."""
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)

    from factory.directions.creator import run_interactive

    created = run_interactive(app=app_name, software_factory_root=_FACTORY_ROOT)
    console.print(f"\n[bold green]Direction created:[/bold green] {created.dir_path}")


@app.command("tell")
def tell(
    app_name: str = typer.Option(..., "--app", help="App name (e.g. sacrifice)"),
    text: str = typer.Argument(..., help='Direction text (e.g. "fix the broken submit button").'),
) -> None:
    """Append a prose-only direction (no flow/api_spec). PM will likely flag needs-direction."""
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)

    from factory.directions.creator import create_direction

    title = text.strip().split("\n", 1)[0][:80]
    created = create_direction(
        app=app_name,
        title=title,
        type_tag=None,
        why=text.strip(),
        has_ui=False,
        flow_steps=None,
        has_api=False,
        api_spec_lines=None,
        acceptance=[],
        explore=False,
        attach_files=None,
        software_factory_root=_FACTORY_ROOT,
        source="cli-tell",
    )
    console.print(
        Panel.fit(
            f"Captured at [bold]{created.dir_path}[/bold].\n"
            "Backpressure is intentionally thin — PM will likely flag this as "
            "[bold]needs-direction[/bold] on the next pm-sync.",
            title="tell",
            style="yellow",
        )
    )


@app.command("edit-direction")
def edit_direction(
    app_name: str = typer.Option(..., "--app", help="App name"),
    id_or_slug: str = typer.Argument(..., help="Direction id (e.g. 003) or slug or 'id-slug'"),
) -> None:
    """Open the direction's ``direction.md`` in ``$EDITOR``."""
    directions_dir = _FACTORY_ROOT / "apps" / app_name / "directions"
    if not directions_dir.exists():
        console.print(f"[red]error:[/red] no directions/ for app {app_name!r}")
        raise typer.Exit(code=2)

    target: Path | None = None
    candidates = [p for p in directions_dir.iterdir() if p.is_dir()]
    for c in candidates:
        if (
            c.name == id_or_slug
            or c.name.startswith(f"{id_or_slug}-")
            or c.name.endswith(f"-{id_or_slug}")
        ):
            target = c
            break
    if target is None:
        console.print(f"[red]error:[/red] no direction matched {id_or_slug!r}")
        raise typer.Exit(code=2)

    direction_md = target / "direction.md"
    if not direction_md.exists():
        console.print(f"[red]error:[/red] {direction_md} missing")
        raise typer.Exit(code=2)

    editor = os.environ.get("EDITOR", "vi")
    try:
        subprocess.run([editor, str(direction_md)], check=False)
    except FileNotFoundError:
        console.print(f"[red]error:[/red] editor {editor!r} not found")
        raise typer.Exit(code=2) from None

    console.print("\n[bold]Directory contents:[/bold]")
    for p in sorted(target.iterdir()):
        console.print(f"  - {p.name}")


@app.command("pm-sync")
def pm_sync_cmd(
    app_name: str = typer.Option(..., "--app", help="App name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Skip LLM + GitHub calls"),
) -> None:
    """Run the PM-sync pipeline for ``--app``. Validates pending directions."""
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)

    from factory.chain.pm_sync import pm_sync

    github_client: Any = None
    if not dry_run:
        # Verify both keys early — fail fast with a clear message.
        if not (os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")):
            console.print(
                "[red]error:[/red] real pm-sync requires DEEPSEEK_API_KEY (or "
                "ANTHROPIC_API_KEY) for the PM persona LLM call. Pass --dry-run "
                "to skip the call."
            )
            raise typer.Exit(code=2)
        github_client = _ensure_github_client()

    summary = pm_sync(
        app=app_name,
        software_factory_root=_FACTORY_ROOT,
        dry_run=dry_run,
        github_client=github_client,
    )

    table = Table(title=f"pm-sync — app={app_name} dry_run={dry_run}")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("processed", str(summary.processed))
    table.add_row("validated", str(summary.validated))
    table.add_row("needs_direction", str(summary.needs_direction))
    table.add_row("errors", str(len(summary.errors)))
    console.print(table)
    if summary.errors:
        console.print("[red]errors:[/red]")
        for did, msg in summary.errors:
            console.print(f"  - {did}: {msg}")
        raise typer.Exit(code=1)


@app.command("ingest-issue")
def ingest_issue(
    app_name: str = typer.Option(..., "--app", help="App name"),
    issue_number: int = typer.Argument(..., help="GitHub issue number to ingest"),
) -> None:
    """Manually ingest a GitHub direction issue into a local direction dir."""
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)

    from factory.directions.ingester import ingest_github_direction_issue

    github_client = _ensure_github_client()
    direction = ingest_github_direction_issue(
        issue_number=issue_number,
        app=app_name,
        software_factory_root=_FACTORY_ROOT,
        github_client=github_client,
    )
    console.print(
        Panel.fit(
            f"Ingested issue [bold]#{issue_number}[/bold] → [bold]{direction.dir_path}[/bold]",
            title="ingest-issue",
            style="green",
        )
    )


# --------------------------------------------------------------------------- #
# Phase-2 commands: tick (drive the chain forward), story (inspect),
# webhook-serve (boot the FastAPI receiver).
# --------------------------------------------------------------------------- #


@app.command("tick")
def tick_cmd(
    app_name: str = typer.Option(..., "--app", help="App name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="No LLM/GitHub/repo writes"),
) -> None:
    """Drive every in-flight story for ``--app`` one tick forward."""
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)

    from factory.chain.orchestrator import tick

    if not dry_run and not (
        os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    ):
        console.print(
            "[red]error:[/red] real tick requires DEEPSEEK_API_KEY (or "
            "ANTHROPIC_API_KEY). Pass --dry-run for offline mode."
        )
        raise typer.Exit(code=2)

    summary = tick(_FACTORY_ROOT, app_name, dry_run=dry_run)

    if not summary.handler_runs and not summary.errors and not summary.rejected:
        console.print(
            Panel.fit(
                f"No in-flight stories for app=[bold]{app_name}[/bold]. "
                "Run [bold]factory pm-sync --app <app>[/bold] first to spawn stories.",
                title="tick",
            )
        )
        return

    table = Table(title=f"tick — app={app_name} dry_run={dry_run}")
    table.add_column("story")
    table.add_column("from")
    table.add_column("to")
    for slug, frm, to in summary.handler_runs:
        table.add_row(slug, frm, to)
    if summary.handler_runs:
        console.print(table)
    if summary.rejected:
        rej_table = Table(title="rejected by caps/mode")
        rej_table.add_column("story")
        rej_table.add_column("reason")
        for slug, reason in summary.rejected:
            rej_table.add_row(slug, reason)
        console.print(rej_table)
    console.print(
        f"advanced={summary.stories_advanced} "
        f"blocked_by_caps={summary.blocked_by_caps} "
        f"blocked={summary.stories_blocked} "
        f"errors={len(summary.errors)}"
    )
    if summary.errors:
        for slug, msg in summary.errors:
            console.print(f"[red]  - {slug}: {msg}[/red]")
        raise typer.Exit(code=1)


@app.command("story")
def story_cmd(
    story_id: int = typer.Argument(..., help="Story id (StoryRecord.id)"),
) -> None:
    """Show a story's current state + handler outputs."""
    from sqlmodel import Session, create_engine

    from factory.chain.state_machine import StoryRecord

    db = _FACTORY_ROOT / "state" / "factory.db"
    if not db.exists():
        console.print("[red]error:[/red] no state db; run pm-sync first")
        raise typer.Exit(code=2)
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        story = session.get(StoryRecord, story_id)
    if story is None:
        console.print(f"[red]error:[/red] no story with id={story_id}")
        raise typer.Exit(code=2)

    console.print(
        Panel.fit(
            f"id=[bold]{story.id}[/bold]  app=[bold]{story.app}[/bold]  "
            f"slug=[bold]{story.slug}[/bold]\n"
            f"state=[bold]{story.state}[/bold]  scope=[bold]{story.scope}[/bold]  "
            f"retries=[bold]{story.dev_retries}[/bold]  tier=[bold]{story.current_model_tier}[/bold]\n"
            f"branch={story.github_branch}  pr=#{story.github_pr_number}  "
            f"issue=#{story.github_issue_number}\n"
            f"story_file_path={story.story_file_path}\n"
            f"error={story.error}",
            title=f"story #{story.id}",
        )
    )


# --------------------------------------------------------------------------- #
# Phase-3 commands: inbox, queue, pause, resume, mode, budget, why,
# settings, spend.
# --------------------------------------------------------------------------- #


def _list_apps() -> list[str]:
    apps_dir = _FACTORY_ROOT / "apps"
    if not apps_dir.exists():
        return []
    return sorted(p.name for p in apps_dir.iterdir() if (p / "config.yaml").exists())


@app.command("inbox")
def inbox_cmd(
    app_name: str | None = typer.Option(
        None, "--app", help="Filter inbox to a single app; default: all apps"
    ),
) -> None:
    """Aggregate items needing human attention across apps."""
    from sqlmodel import Session, create_engine, select

    from factory.chain.handlers import _engine
    from factory.chain.state_machine import StoryRecord
    from factory.directions.parser import list_direction_dirs, parse_direction_dir
    from factory.settings.loader import load_settings
    from factory.settings.modes import get_mode
    from factory.settings.spend import today_spend_usd

    settings = load_settings(_FACTORY_ROOT)
    db = _FACTORY_ROOT / "state" / "factory.db"
    _engine(db)
    apps = [app_name] if app_name else _list_apps()

    # Stories with last_rejection_reason or in BLOCKED state -> needs human.
    eng = create_engine(f"sqlite:///{db}", echo=False)
    needs_human_table = Table(title="Needs human action (stories)")
    needs_human_table.add_column("app")
    needs_human_table.add_column("id")
    needs_human_table.add_column("slug")
    needs_human_table.add_column("state")
    needs_human_table.add_column("reason / blocker")
    have_needs = False
    with Session(eng) as session:
        for a in apps:
            rows = session.exec(select(StoryRecord).where(StoryRecord.app == a)).all()
            for r in rows:
                reason: str | None = None
                if r.last_rejection_reason:
                    reason = r.last_rejection_reason
                elif r.state in {"blocked_tests_need_clarification", "reviewer_requested_changes"}:
                    reason = r.state
                if reason:
                    needs_human_table.add_row(a, str(r.id), r.slug, r.state, reason)
                    have_needs = True
    if have_needs:
        console.print(needs_human_table)
    else:
        console.print("[dim]No stories awaiting human action.[/dim]")

    # needs-direction status from direction state.yaml.
    nd_table = Table(title="needs-direction (directions)")
    nd_table.add_column("app")
    nd_table.add_column("direction")
    nd_table.add_column("title")
    nd_table.add_column("missing")
    have_nd = False
    for a in apps:
        for ddir in list_direction_dirs(a, _FACTORY_ROOT):
            try:
                d = parse_direction_dir(a, ddir)
            except Exception:
                continue
            if d.status == "needs-direction":
                nd_table.add_row(
                    a, ddir.name, d.title[:60], ", ".join(d.state.get("missing") or [])
                )
                have_nd = True
    if have_nd:
        console.print(nd_table)
    else:
        console.print("[dim]No directions in needs-direction.[/dim]")

    # Budget warning.
    spend = today_spend_usd(_FACTORY_ROOT, db_path=db)
    cap = settings.caps.daily_spend_usd
    if cap > 0 and spend >= cap * 0.75:
        console.print(
            Panel.fit(
                f"[yellow]Budget warning:[/yellow] today's spend ${spend:.4f} >= 75% of "
                f"daily cap ${cap:.2f}",
                title="budget",
            )
        )

    # Direction trackers awaiting action (status == 'pm-validated' but no
    # downstream story yet) — rough heuristic; full Phase 7 will be richer.
    trk_table = Table(title="active direction trackers")
    trk_table.add_column("app")
    trk_table.add_column("direction")
    trk_table.add_column("status")
    have_trk = False
    for a in apps:
        for ddir in list_direction_dirs(a, _FACTORY_ROOT):
            try:
                d = parse_direction_dir(a, ddir)
            except Exception:
                continue
            if d.status not in {"created", "needs-direction"}:
                trk_table.add_row(a, ddir.name, d.status)
                have_trk = True
    if have_trk:
        console.print(trk_table)

    console.print(
        f"[dim]Current factory mode: [bold]{get_mode(_FACTORY_ROOT, db_path=db)}[/bold][/dim]"
    )


@app.command("queue")
def queue_cmd(
    app_name: str | None = typer.Option(None, "--app", help="Filter to one app"),
) -> None:
    """List in-flight StoryRecords with their state + last rejection reason."""
    from sqlmodel import Session, create_engine, select

    from factory.chain.handlers import _engine
    from factory.chain.state_machine import StoryRecord, StoryState

    db = _FACTORY_ROOT / "state" / "factory.db"
    _engine(db)
    terminal = {
        StoryState.PR_OPEN.value,
        StoryState.CI_PENDING.value,
        StoryState.CI_GREEN.value,
        StoryState.READY_FOR_MERGE.value,
        StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value,
    }
    eng = create_engine(f"sqlite:///{db}", echo=False)
    table = Table(title="queue (in-flight stories)")
    table.add_column("id")
    table.add_column("app")
    table.add_column("slug")
    table.add_column("state")
    table.add_column("retries")
    table.add_column("rejection")
    with Session(eng) as session:
        stmt = select(StoryRecord)
        if app_name:
            stmt = stmt.where(StoryRecord.app == app_name)
        rows = session.exec(stmt).all()
    for r in rows:
        if r.state in terminal:
            continue
        table.add_row(
            str(r.id),
            r.app,
            r.slug,
            r.state,
            str(r.dev_retries),
            r.last_rejection_reason or "",
        )
    console.print(table)


@app.command("pause")
def pause_cmd() -> None:
    """Halt new dispatches: sets factory mode to ``paused``."""
    from factory.settings.modes import set_mode

    new = set_mode("paused", _FACTORY_ROOT)
    console.print(Panel.fit(f"factory mode -> [bold yellow]{new}[/bold yellow]", title="pause"))


@app.command("resume")
def resume_cmd() -> None:
    """Restore normal operation: sets factory mode to ``normal``."""
    from factory.settings.modes import set_mode

    new = set_mode("normal", _FACTORY_ROOT)
    console.print(Panel.fit(f"factory mode -> [bold green]{new}[/bold green]", title="resume"))


@app.command("mode")
def mode_cmd(
    name: str | None = typer.Argument(None, help="Mode name; omit to print the current mode"),
) -> None:
    """Show or set the factory mode."""
    from factory.settings.loader import is_valid_mode, load_settings
    from factory.settings.modes import get_mode, set_mode

    settings = load_settings(_FACTORY_ROOT)
    if name is None:
        current = get_mode(_FACTORY_ROOT)
        console.print(
            f"current mode: [bold]{current}[/bold]\n"
            f"available: {', '.join(settings.modes.available)}"
        )
        return
    if not is_valid_mode(name, settings):
        console.print(
            f"[red]error:[/red] mode {name!r} not in allowed set: "
            f"{', '.join(settings.modes.available)}"
        )
        raise typer.Exit(code=2)
    new = set_mode(name, _FACTORY_ROOT, settings=settings)
    console.print(Panel.fit(f"factory mode -> [bold]{new}[/bold]", title="mode"))


@app.command("budget")
def budget_cmd() -> None:
    """Show today's spend, hourly spend, projected end-of-day, last 5 runs."""
    from factory.settings.loader import load_settings
    from factory.settings.spend import (
        hour_spend_usd,
        projected_end_of_day,
        recent_runs,
        today_spend_usd,
    )

    settings = load_settings(_FACTORY_ROOT)
    db = _FACTORY_ROOT / "state" / "factory.db"
    today = today_spend_usd(_FACTORY_ROOT, db_path=db)
    hour = hour_spend_usd(_FACTORY_ROOT, db_path=db)
    proj = projected_end_of_day(_FACTORY_ROOT, db_path=db)
    daily_cap = settings.caps.daily_spend_usd
    hourly_cap = settings.caps.hourly_spend_usd
    table = Table(title="budget")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("today_spend_usd", f"${today:.4f}")
    table.add_row("daily_cap_usd", f"${daily_cap:.4f}")
    table.add_row("hour_spend_usd", f"${hour:.4f}")
    table.add_row("hourly_cap_usd", f"${hourly_cap:.4f}")
    table.add_row("projected_end_of_day_usd", f"${proj:.4f}")
    console.print(table)
    runs = recent_runs(_FACTORY_ROOT, db_path=db, limit=5)
    if runs:
        rtable = Table(title="last 5 runs")
        rtable.add_column("ts")
        rtable.add_column("persona")
        rtable.add_column("model")
        rtable.add_column("cost_usd", justify="right")
        for r in runs:
            rtable.add_row(r.ts, r.persona, r.model, f"${(r.cost_usd or 0):.4f}")
        console.print(rtable)


@app.command("why")
def why_cmd(
    target: str = typer.Argument(..., help="Story id (StoryRecord.id) or slug"),
) -> None:
    """Explain why a story is stuck/blocked.

    Projects the next-tick decision by looking up the orchestrator's
    ``_DISPATCH[story.state]`` handler kind and running ``can_dispatch``
    against the same ``current_state`` dict the orchestrator builds. This
    answers the operator's actual question ("would the next tick advance
    this story, or block it, and why?") rather than just echoing the last
    historical rejection.
    """
    from sqlmodel import Session, create_engine, select

    from factory.chain.handlers import _engine
    from factory.chain.handlers import stories_in_flight as _in_flight
    from factory.chain.orchestrator import _DISPATCH, _build_current_state
    from factory.chain.state_machine import StoryRecord, StoryState, list_transitions_from
    from factory.settings.enforcer import can_dispatch
    from factory.settings.loader import load_settings

    db = _FACTORY_ROOT / "state" / "factory.db"
    _engine(db)
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        story: StoryRecord | None = None
        try:
            story = session.get(StoryRecord, int(target))
        except ValueError:
            rows = session.exec(select(StoryRecord).where(StoryRecord.slug == target)).all()
            if rows:
                story = rows[0]
        if story is None:
            console.print(f"[red]error:[/red] no story matched {target!r}")
            raise typer.Exit(code=2)

    next_edges = list_transitions_from(StoryState(story.state))
    next_edges_str = (
        ", ".join(f"{ev} -> {ns.value}" for ev, ns in next_edges) if next_edges else "(terminal)"
    )

    # Project the next tick: which handler kind would fire, and would the
    # enforcer allow it?
    projection_line = "next-tick projection: (terminal — no handler dispatches from here)"
    handler_name = _DISPATCH.get(StoryState(story.state))
    if handler_name is not None:
        # Resolve the actual job_kind the orchestrator would pass to the
        # enforcer; ``_resolve_job_kind`` handles bug-suffix routing for
        # bug-typed directions/stories.
        from factory.chain.handlers import find_direction_for_story
        from factory.chain.orchestrator import _resolve_job_kind

        direction = find_direction_for_story(story, _FACTORY_ROOT)
        job_kind = _resolve_job_kind(story, direction, handler_name)

        in_flight_app = max(0, len(_in_flight(story.app, db)) - 1)
        settings = load_settings(_FACTORY_ROOT)
        state_dict = _build_current_state(
            root=_FACTORY_ROOT,
            db=db,
            app=story.app,
            in_flight_app=in_flight_app,
            exclude_story_id=story.id,
        )
        decision = can_dispatch(job_kind, story.app, state_dict, settings)
        if decision.allowed:
            projection_line = (
                f"next-tick projection: [bold green]would dispatch[/bold green] "
                f"job_kind=[bold]{job_kind}[/bold]"
            )
        else:
            projection_line = (
                f"next-tick projection: [bold red]would be blocked[/bold red]: "
                f"[bold]{decision.rejected_reason}[/bold] (job_kind={job_kind})"
            )

    lines = [
        f"id=[bold]{story.id}[/bold]  slug=[bold]{story.slug}[/bold]",
        f"app=[bold]{story.app}[/bold]  state=[bold]{story.state}[/bold]",
        f"retries=[bold]{story.dev_retries}[/bold]  tier=[bold]{story.current_model_tier}[/bold]",
        f"last_rejection_reason=[bold]{story.last_rejection_reason or '(none)'}[/bold]",
        f"error=[bold]{story.error or '(none)'}[/bold]",
        f"branch={story.github_branch}  pr=#{story.github_pr_number}",
        f"next legal transitions: {next_edges_str}",
        projection_line,
    ]
    console.print(Panel("\n".join(lines), title=f"why story {story.id}"))


@app.command("settings")
def settings_cmd() -> None:
    """Pretty-print the loaded factory_settings.yaml."""
    from factory.settings.loader import load_settings

    settings = load_settings(_FACTORY_ROOT)
    import json as _json

    console.print(Panel(_json.dumps(settings.model_dump(), indent=2), title="factory settings"))


@app.command("spend")
def spend_cmd(
    days: int = typer.Option(7, "--days", help="Days of history to show"),
) -> None:
    """Historical spend breakdown (last N days)."""
    from factory.settings.spend import spend_by_day

    rows = spend_by_day(_FACTORY_ROOT, days=days)
    table = Table(title=f"spend (last {days} days)")
    table.add_column("date")
    table.add_column("usd", justify="right")
    total = 0.0
    for d, usd in rows:
        table.add_row(d, f"${usd:.4f}")
        total += usd
    table.add_row("total", f"${total:.4f}")
    console.print(table)


@app.command("webhook-serve")
def webhook_serve(
    port: int = typer.Option(9000, "--port", help="Bind port for the webhook listener"),
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host"),
) -> None:
    """Boot the GitHub webhook receiver via uvicorn."""
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)
    try:
        import uvicorn
    except ImportError as exc:
        console.print(f"[red]error:[/red] uvicorn not installed: {exc}")
        raise typer.Exit(code=2) from exc

    if not os.environ.get("GITHUB_WEBHOOK_SECRET"):
        console.print(
            "[yellow]warning:[/yellow] GITHUB_WEBHOOK_SECRET is unset. "
            "Webhooks will be rejected with 503 until you set it."
        )

    console.print(
        Panel.fit(
            f"Booting webhook listener on [bold]{host}:{port}[/bold]\n"
            "For local dev with smee:\n"
            f"  smee --port {port} --url <smee URL>\n"
            "POST endpoint: /webhook/github  •  Health: /health",
            title="webhook-serve",
        )
    )
    uvicorn.run("factory.webhook.github:app", host=host, port=port, log_level="info")
