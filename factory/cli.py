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
    """Construct a ``pygithub.Github`` client. Fails with a clear message if no token.

    Token precedence: ``GITHUB_TOKEN`` env > ``GH_TOKEN`` env > ``gh auth token``.
    The ``gh`` CLI fallback means an operator who has run ``gh auth login`` once
    does not need to also paste the token into ``.env``.
    """
    from factory.providers.github import resolve_github_token

    token = resolve_github_token()
    if not token:
        console.print(
            "[red]error:[/red] no GitHub token available. Either set "
            "[bold]GITHUB_TOKEN[/bold] (or [bold]GH_TOKEN[/bold]) in the "
            "environment / .env, or run [bold]gh auth login[/bold] so "
            "[bold]gh auth token[/bold] returns one."
        )
        raise typer.Exit(code=2)
    from github import Github

    return Github(token)


def _has_any_llm_provider_key() -> tuple[bool, str]:
    """Return ``(has_key, hint)`` describing whether SOME LLM provider is usable.

    The pre-check that used to look for ``DEEPSEEK_API_KEY`` / ``ANTHROPIC_API_KEY``
    predates Azure being the default provider. With ``default_provider: azure``
    the relevant env vars are ``AZURE_API_KEY`` (Azure-OpenAI) or
    ``AZURE_AI_API_KEY`` / ``AZURE_FOUNDRY_API_KEY`` (Foundry). Accept any of
    them — the runner picks the right key per model via ``_provider_env_key``.

    The hint string is shown to the operator on failure.
    """
    candidates = (
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AZURE_API_KEY",
        "AZURE_AI_API_KEY",
        "AZURE_FOUNDRY_API_KEY",
    )
    for name in candidates:
        if os.environ.get(name):
            return True, ""
    hint = (
        "set one of "
        + ", ".join(f"[bold]{n}[/bold]" for n in candidates)
        + " in .env. Pass [bold]--dry-run[/bold] for offline mode."
    )
    return False, hint


@app.command("new-direction")
def new_direction(
    app_name: str = typer.Option(..., "--app", help="App name (under apps/<name>/)"),
) -> None:
    """Interactive direction creation. Walks the user through the prompts."""
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)

    from factory.directions.creator import run_interactive

    created = run_interactive(app=app_name, software_factory_root=_FACTORY_ROOT)
    console.print(f"\n[bold green]Direction created:[/bold green] {created.dir_path}")


@app.command("tell")
def tell(
    app_name: str = typer.Option(..., "--app", help="App name (under apps/<name>/)"),
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
        # Verify SOME provider key is set; the runner picks the right one per
        # model. With ``default_provider: azure``, AZURE_API_KEY is the usual
        # answer; legacy direct-provider runs still accept DEEPSEEK / ANTHROPIC.
        ok, hint = _has_any_llm_provider_key()
        if not ok:
            console.print("[red]error:[/red] real pm-sync requires an LLM provider key. " + hint)
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

    if not dry_run:
        ok, hint = _has_any_llm_provider_key()
        if not ok:
            console.print("[red]error:[/red] real tick requires an LLM provider key. " + hint)
            raise typer.Exit(code=2)

    # Phase 6: drive the scheduler BEFORE the story chain so findings
    # filed by Ralph/etc become directions that this same tick can pick
    # up (PM-sync runs separately, but the next tick will spawn stories
    # from them).
    from factory.chain.scheduled_tasks import run_scheduled_persona
    from factory.scheduler.cron import due_schedules

    scheduled_results = []

    # Event-triggered factory_improver. Every tick, ask whether a
    # ``factory_needs_redesign`` event has landed since the last
    # improver run AND the debounce window has elapsed AND we're under
    # the daily cap. Fires inside the same tick so the L2 apply pass
    # can land a fix within minutes of the originating failure, not
    # hours.
    if not dry_run:
        from factory.chain.factory_improver import (
            record_improver_fired,
            run_factory_improver,
            should_fire_improver,
        )

        try:
            from factory.settings.loader import load_settings

            settings = load_settings(_FACTORY_ROOT)
            daily_cap_imp = int(getattr(settings.rate_limits, "factory_improver_runs_per_day", 12))
        except Exception:
            daily_cap_imp = 12
        fire, reason = should_fire_improver(
            software_factory_root=_FACTORY_ROOT,
            daily_cap=daily_cap_imp,
        )
        if fire:
            try:
                cfg_for_issue = None
                try:
                    from factory.app_config import load_app_config

                    cfg_for_issue = load_app_config(app_name, _FACTORY_ROOT).repo
                except Exception:
                    cfg_for_issue = None
                record_improver_fired(_FACTORY_ROOT)
                fi_out = run_factory_improver(
                    app=app_name,
                    software_factory_root=_FACTORY_ROOT,
                    dry_run=False,
                    repo_for_issue=cfg_for_issue,
                    apply_pass=True,
                    apply_repo="xvanov/software-factory",
                )
                scheduled_results.append(
                    (
                        "factory_improver (event)",
                        "ok" if fi_out.succeeded else "errored",
                        fi_out.events_processed,
                        fi_out.improvements_count,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - never fail the tick
                scheduled_results.append(
                    ("factory_improver (event)", f"errored:{exc!r}"[:60], 0, 0)
                )
        else:
            scheduled_results.append(("factory_improver (event)", f"skipped:{reason}", 0, 0))

    for due in due_schedules(_FACTORY_ROOT, audit_app=app_name):
        if due.rate_limit_hit:
            scheduled_results.append((due.schedule.name, "rate_limited", 0, 0))
            continue
        # ``factory_improver`` used to be in this list under a daily
        # cron; it's now event-triggered above. Skip any stale entry
        # an operator may still have in their YAML.
        if due.schedule.persona == "factory_improver":
            continue
        out = run_scheduled_persona(
            due.schedule.persona,
            app_name,
            _FACTORY_ROOT,
            dry_run=dry_run,
        )
        scheduled_results.append(
            (due.schedule.name, out.status, out.findings_count, len(out.directions_filed))
        )
    if scheduled_results:
        sched_table = Table(title="scheduled personas fired this tick")
        sched_table.add_column("schedule")
        sched_table.add_column("status")
        sched_table.add_column("findings")
        sched_table.add_column("directions")
        for name, status, fcount, dcount in scheduled_results:
            sched_table.add_row(name, status, str(fcount), str(dcount))
        console.print(sched_table)

    summary = tick(_FACTORY_ROOT, app_name, dry_run=dry_run)

    # After the story chain advances, drain any pending deploy queue
    # entries for this app. The deploy worker honors deploy-frozen mode
    # via the settings enforcer.
    from factory.deploy.orchestrator import drain_deploy_queue

    deploy_actions = drain_deploy_queue(
        app=app_name,
        software_factory_root=_FACTORY_ROOT,
        dry_run=dry_run,
    )
    if deploy_actions:
        dep_table = Table(title="deploy queue drained")
        dep_table.add_column("sha")
        dep_table.add_column("status")
        for a in deploy_actions:
            derived = (
                "deployed"
                if a.success
                else (
                    "rolled_back"
                    if a.rolled_back
                    else (
                        "skipped"
                        if a.error
                        and (
                            a.error in {"mode_blocks_deploy", "deploy_disabled_in_config"}
                            or a.error.startswith("mode_")
                        )
                        else "errored"
                    )
                )
            )
            dep_table.add_row(a.merged_sha[:12], derived)
        console.print(dep_table)

    if (
        not summary.handler_runs
        and not summary.errors
        and not summary.rejected
        and not summary.merges
    ):
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
    if summary.merges:
        merge_table = Table(title="auto-merge decisions this tick")
        merge_table.add_column("pr")
        merge_table.add_column("merged")
        merge_table.add_column("reason")
        merge_table.add_column("gates", justify="right")
        for m in summary.merges:
            merge_table.add_row(
                f"#{m.pr_number}",
                "[green]yes[/green]" if m.merged else "[red]no[/red]",
                m.reason[:80],
                str(len(m.gates_passed)),
            )
        console.print(merge_table)
    console.print(
        f"advanced={summary.stories_advanced} "
        f"blocked_by_caps={summary.blocked_by_caps} "
        f"blocked={summary.stories_blocked} "
        f"merges={sum(1 for m in summary.merges if m.merged)}/{len(summary.merges)} "
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
    """Aggregate items needing human attention across apps.

    Phase 7: shows multi-app rollup of every signal an operator cares
    about — stories awaiting human action (``reviewer_requested_changes``,
    ``blocked_tests_need_clarification``, and any ``last_rejection_reason``),
    directions in ``needs-direction``, budget warnings, failed deploys
    in the last 24h, active Direction Trackers, recent scheduled persona
    runs, idle apps (the same predicate the ``factory-idle`` issue uses),
    and pinned ``factory-status`` issue numbers per app.
    """
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
    # Ensure deploy_actions table exists for the failed-deploys section
    # below; on a fresh checkout no deploys have run yet so SQLModel
    # never auto-created the table from the chain handlers' metadata.
    from factory.deploy.orchestrator import _engine as _deploy_engine

    _deploy_engine(db)
    # Phase 6/7 tables must exist for the scheduled-runs + idle +
    # factory-status sections below; their _engine() helpers run
    # create_all on first call.
    from factory.chain.factory_status import _engine as _status_engine
    from factory.chain.scheduled_tasks import _engine as _sched_engine

    _sched_engine(db)
    _status_engine(db)
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

    # Failed deploys in the last 24h (status='errored' OR rolled_back).
    from datetime import UTC, datetime, timedelta

    from factory.deploy.models import DeployActionRecord

    cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    failed_dep_table = Table(title="failed deploys (last 24h)")
    failed_dep_table.add_column("app")
    failed_dep_table.add_column("sha")
    failed_dep_table.add_column("status")
    failed_dep_table.add_column("error")
    have_failed_dep = False
    with Session(eng) as session:
        for a in apps:
            dep_rows = session.exec(
                select(DeployActionRecord).where(
                    DeployActionRecord.app == a,
                    DeployActionRecord.status.in_(["errored", "rolled_back"]),  # type: ignore[attr-defined]
                    DeployActionRecord.ts >= cutoff,
                )
            ).all()
            for dr in dep_rows:
                failed_dep_table.add_row(a, dr.sha[:12], dr.status, (dr.error or "")[:60])
                have_failed_dep = True
    if have_failed_dep:
        console.print(failed_dep_table)
    else:
        console.print("[dim]No failed deploys in the last 24h.[/dim]")

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

    # Phase 6: recent scheduled runs (last 24h).
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from factory.chain.scheduled_tasks import ScheduledRunRecord

    cutoff_sched = (_dt.now(_UTC) - _td(hours=24)).isoformat()
    sched_table = Table(title="scheduled runs (last 24h)")
    sched_table.add_column("ts")
    sched_table.add_column("persona")
    sched_table.add_column("app")
    sched_table.add_column("findings")
    sched_table.add_column("directions_filed")
    sched_table.add_column("status")
    have_sched = False
    with Session(eng) as session:
        sched_rows = session.exec(
            select(ScheduledRunRecord)
            .where(ScheduledRunRecord.ts >= cutoff_sched)
            .order_by(ScheduledRunRecord.id.desc())  # type: ignore[union-attr]
        ).all()
        for sr in sched_rows:
            sched_table.add_row(
                sr.ts[:19],
                sr.persona,
                sr.app,
                str(sr.findings_count),
                sr.directions_filed_json,
                sr.status,
            )
            have_sched = True
    if have_sched:
        console.print(sched_table)
    else:
        console.print("[dim]No scheduled persona runs in the last 24h.[/dim]")

    # Phase 7: idle pings — apps with no in-flight work, no recent
    # findings, no recent deploys (the same predicate ``factory-idle``
    # uses). These are surfaced in the inbox so the operator sees them
    # without needing to wait for the cron tick to open a GH issue.
    from factory.chain.idle import detect_idle

    idle_table = Table(title="idle apps (no work in flight)")
    idle_table.add_column("app")
    idle_table.add_column("idle since")
    idle_table.add_column("recent directions")
    have_idle = False
    for a in apps:
        try:
            snap = detect_idle(a, _FACTORY_ROOT, since_hours=2)
        except Exception:
            continue
        if snap is None:
            continue
        directions_str = ", ".join(d.slug for d in snap.recent_directions[:3]) or "(none)"
        idle_table.add_row(a, snap.idle_since.isoformat()[:19], directions_str)
        have_idle = True
    if have_idle:
        console.print(idle_table)

    # Phase 7: pinned ``factory-status`` issue numbers (one per app).
    # These are the operators' single GH-side entry point for live state.
    from factory.chain.factory_status import FactoryStatusRecord

    status_table = Table(title="pinned factory-status issues")
    status_table.add_column("app")
    status_table.add_column("issue")
    status_table.add_column("last updated")
    have_status = False
    with Session(eng) as session:
        for a in apps:
            row = session.exec(
                select(FactoryStatusRecord).where(FactoryStatusRecord.app == a)
            ).first()
            if row is None:
                continue
            status_table.add_row(a, f"#{row.gh_issue_number}", row.last_updated[:19])
            have_status = True
    if have_status:
        console.print(status_table)

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

    # Tail the per-story event log so ``factory why`` shows the recent
    # chain decisions inline. Operators usually want the last few events
    # (retry counts, dispatch rejections, exceptions) without separately
    # opening the log file. ``factory trace`` shows the full history.
    from factory.chain.event_log import read_story_events

    recent = read_story_events(
        story.id,
        software_factory_root=_FACTORY_ROOT,
        slug_hint=story.slug,
        limit=8,
    )
    event_lines: list[str] = []
    for ev in recent:
        ts = ev.get("ts", "?")[11:19] if ev.get("ts") else "?"
        kind = ev.get("event", "?")
        extras = {k: v for k, v in ev.items() if k not in {"ts", "story_id", "event"}}
        extras_str = " ".join(f"{k}={v!r}" for k, v in extras.items())[:200]
        event_lines.append(f"  {ts}  {kind}  {extras_str}")
    events_block = (
        "recent events (tail 8 — see ``factory trace`` for full log):\n" + "\n".join(event_lines)
        if event_lines
        else "recent events: (none recorded yet)"
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
        "",
        events_block,
    ]
    console.print(Panel("\n".join(lines), title=f"why story {story.id}"))


@app.command("trace")
def trace_cmd(
    target: str = typer.Argument(..., help="Story id"),
    limit: int = typer.Option(
        50,
        "--limit",
        "-n",
        help="Most-recent N events to show; 0 for full history",
    ),
) -> None:
    """Dump the per-story event log — every handler run, retry, and decision.

    The chain writes one JSONL record per significant event to
    ``state/logs/<story-id>-<slug>.log``. Use this when ``factory why``'s
    tail isn't enough — e.g. tracking down why dev exhausted retries,
    inspecting JSON-mode truncation auto-retries, or auditing what the
    enforcer rejected before dispatch.
    """
    from sqlmodel import Session, create_engine

    from factory.chain.event_log import read_story_events
    from factory.chain.state_machine import StoryRecord

    db = _FACTORY_ROOT / "state" / "factory.db"
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as session:
        try:
            sid = int(target)
        except ValueError:
            console.print(f"[red]error:[/red] expected story id (int); got {target!r}")
            raise typer.Exit(code=2)
        story = session.get(StoryRecord, sid)

    slug = story.slug if story is not None else ""
    events = read_story_events(
        sid,
        software_factory_root=_FACTORY_ROOT,
        slug_hint=slug,
        limit=limit if limit > 0 else None,
    )
    if not events:
        console.print(f"(no events logged for story {sid})")
        return

    table = Table(title=f"trace story {sid} ({slug or '?'}) — {len(events)} events")
    table.add_column("ts")
    table.add_column("event", style="bold")
    table.add_column("detail")
    for ev in events:
        ts = ev.get("ts", "?")[11:23] if ev.get("ts") else "?"
        kind = ev.get("event", "?")
        extras = {k: v for k, v in ev.items() if k not in {"ts", "story_id", "event"}}
        # Compact one-line detail; long values clipped.
        parts = []
        for k, v in extras.items():
            s = repr(v) if not isinstance(v, str) else v
            parts.append(f"{k}={s[:160]}")
        table.add_row(ts, kind, " ".join(parts))
    console.print(table)


@app.command("settings")
def settings_cmd() -> None:
    """Pretty-print the loaded factory_settings.yaml."""
    from factory.settings.loader import load_settings

    settings = load_settings(_FACTORY_ROOT)
    import json as _json

    console.print(Panel(_json.dumps(settings.model_dump(), indent=2), title="factory settings"))


@app.command("tui")
def tui_cmd(
    app_name: str | None = typer.Option(
        None, "--app", help="Filter the dashboard to a single app; default: all apps"
    ),
    refresh: float = typer.Option(1.0, "--refresh", help="Refresh interval in seconds (>= 0.25)"),
    recompute_baselines: bool = typer.Option(
        False,
        "--recompute-baselines",
        help="Recompute (persona, points) baselines from history before launching",
    ),
) -> None:
    """Launch the live terminal dashboard (factory's ``nvidia-smi``).

    Reads ``state/factory.db`` and refreshes every ``--refresh`` seconds.
    Shows mode, 24h/7d spend, per-app stats, in-flight directions with
    EBS Monte Carlo ETAs (P50/P75/P95), mid-flight personas, velocity
    per (persona, model_tier), and a tail of recent runs.

    Keys: ``q`` quit, ``r`` recompute baselines on demand.
    """
    from factory.settings.loader import load_settings
    from factory.tui import run_tui

    db = _FACTORY_ROOT / "state" / "factory.db"
    if recompute_baselines:
        from factory.observability.estimator import recompute_baselines as _rb

        n = _rb(db)
        console.print(f"[dim]recomputed {n} (persona, points) baselines[/dim]")

    spend_cap: float | None = None
    try:
        settings = load_settings(_FACTORY_ROOT)
        spend_cap = float(settings.caps.daily_spend_usd) or None
    except Exception:
        spend_cap = None

    run_tui(
        software_factory_root=_FACTORY_ROOT,
        db_path=db,
        spend_cap_usd=spend_cap,
        app_filter=app_name,
        refresh_seconds=max(0.25, refresh),
    )


@app.command("baselines")
def baselines_cmd() -> None:
    """Recompute EBS handler baselines from the runs ⨝ stories history.

    Refreshes ``handler_baselines`` (per-(persona, points) median seconds).
    Safe to call any time; the TUI also exposes this on the ``r`` key.
    """
    from factory.observability.estimator import recompute_baselines

    db = _FACTORY_ROOT / "state" / "factory.db"
    n = recompute_baselines(db)
    console.print(f"recomputed [bold]{n}[/bold] (persona, points) baselines")


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


@app.command("auto-merge")
def auto_merge_cmd(
    app_name: str = typer.Option(..., "--app", help="App name"),
    dry_run: bool = typer.Option(True, "--dry-run/--real-run", help="Dry-run (default)"),
) -> None:
    """Run one auto-merge tick against ``--app``.

    In dry-run mode the worker reads the local DB only and does not call
    GitHub. Use ``--real-run`` to invoke the GH client (requires GITHUB_TOKEN).
    """
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)

    from factory.chain.auto_merge import auto_merge_tick

    gh: Any = None
    if not dry_run:
        gh = _ensure_github_client()

    actions = auto_merge_tick(_FACTORY_ROOT, app_name, dry_run=dry_run, github_client=gh)

    if not actions:
        console.print(
            Panel.fit(
                f"No PR fixtures or open PRs to evaluate for [bold]{app_name}[/bold].",
                title="auto-merge",
            )
        )
        return

    table = Table(title=f"auto-merge — app={app_name} dry_run={dry_run}")
    table.add_column("pr")
    table.add_column("merged")
    table.add_column("reason")
    table.add_column("gates_passed", justify="right")
    for a in actions:
        table.add_row(
            f"#{a.pr_number}",
            "[green]yes[/green]" if a.merged else "[red]no[/red]",
            a.reason[:80],
            str(len(a.gates_passed)),
        )
    console.print(table)


@app.command("rollback-watch")
def rollback_watch_cmd(
    app_name: str = typer.Option(..., "--app", help="App name"),
    dry_run: bool = typer.Option(True, "--dry-run/--real-run", help="Dry-run (default)"),
    window_minutes: int = typer.Option(
        15, "--window-minutes", help="How far back to look for recent merges"
    ),
) -> None:
    """Run one rollback-watch tick: look at recent merges; revert if main CI is red."""
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)

    from factory.chain.rollback import rollback_watch_tick

    gh: Any = None
    if not dry_run:
        gh = _ensure_github_client()

    actions = rollback_watch_tick(
        _FACTORY_ROOT,
        app_name,
        dry_run=dry_run,
        github_client=gh,
        window_minutes=window_minutes,
    )

    if not actions:
        console.print(
            Panel.fit(
                f"No recent merges to evaluate for [bold]{app_name}[/bold] "
                f"(last {window_minutes} min).",
                title="rollback-watch",
            )
        )
        return

    table = Table(title=f"rollback-watch — app={app_name} dry_run={dry_run}")
    table.add_column("pr")
    table.add_column("action")
    table.add_column("reason")
    for a in actions:
        table.add_row(
            f"#{a.pr_number}",
            a.action_type,
            a.reason[:80],
        )
    console.print(table)


@app.command("deploy")
def deploy_cmd(
    app_name: str = typer.Option(..., "--app", help="App name"),
    sha: str | None = typer.Option(None, "--sha", help="Specific SHA to deploy (overrides queue)"),
    pr: int | None = typer.Option(
        None, "--pr", help="Deploy a specific PR (uses placeholder SHA in dry-run)"
    ),
    dry_run: bool = typer.Option(True, "--dry-run/--real-run", help="Dry-run (default)"),
) -> None:
    """Run one deploy tick against ``--app``.

    In dry-run mode no subprocesses are launched — every step is
    deterministically successful. Use ``--real-run`` to actually invoke
    the configured deploy commands.

    Pass ``--pr <number>`` to target a specific PR; in dry-run this uses
    a placeholder SHA so the operator can exercise the flow without a
    matching ``merge_actions`` row. ``--sha`` is preferred for real-run.
    """
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)

    from factory.deploy.orchestrator import (
        deploy_action_as_dict,
        deploy_post_merge,
        deploy_tick,
    )

    gh: Any = None
    if not dry_run:
        gh = _ensure_github_client()

    actions: list[Any]
    if pr is not None:
        action = deploy_post_merge(
            app_name,
            pr,
            sha or f"pr-{pr}-placeholder-sha",
            _FACTORY_ROOT,
            dry_run=dry_run,
            github_client=gh,
        )
        actions = [action]
    else:
        actions = deploy_tick(
            _FACTORY_ROOT,
            app_name,
            dry_run=dry_run,
            sha=sha,
            github_client=gh,
        )

    if not actions:
        console.print(
            Panel.fit(
                f"No candidate SHA to deploy for [bold]{app_name}[/bold]. "
                f"Merge a PR first or pass --sha <sha>.",
                title="deploy",
            )
        )
        return

    table = Table(title=f"deploy — app={app_name} dry_run={dry_run}")
    table.add_column("sha")
    table.add_column("status")
    table.add_column("smoke")
    table.add_column("rolled_back")
    table.add_column("error")
    for a in actions:
        d = deploy_action_as_dict(a)
        status_color = {
            True: "green",
        }.get(a.success, "yellow" if a.rolled_back else "red")
        derived_status = (
            "deployed" if a.success else ("rolled_back" if a.rolled_back else "errored")
        )
        # ``mode_blocks_deploy`` / ``deploy_disabled_in_config`` collapse
        # to "skipped" in DB; surface here too.
        if a.error in {"mode_blocks_deploy", "deploy_disabled_in_config"} or (
            a.error and a.error.startswith("mode_")
        ):
            derived_status = "skipped"
            status_color = "yellow"
        table.add_row(
            d["merged_sha"][:12],
            f"[{status_color}]{derived_status}[/{status_color}]",
            "yes" if a.smoke_passed else "no",
            "yes" if a.rolled_back else "no",
            (a.error or "")[:60],
        )
    console.print(table)


@app.command("deploys")
def deploys_cmd(
    app_name: str = typer.Option(..., "--app", help="App name"),
    limit: int = typer.Option(20, "--limit", help="Max rows to show (newest first)"),
) -> None:
    """List recent ``DeployAction`` rows for ``--app``."""
    from sqlmodel import Session, select

    from factory.deploy.models import DeployActionRecord
    from factory.deploy.orchestrator import _engine as _deploy_engine

    db = _FACTORY_ROOT / "state" / "factory.db"
    # ``_deploy_engine`` runs ``SQLModel.metadata.create_all`` so the
    # ``deploy_actions`` table exists even on a fresh checkout that has
    # never run a deploy yet.
    eng = _deploy_engine(db)
    with Session(eng) as session:
        rows = list(
            session.exec(
                select(DeployActionRecord)
                .where(DeployActionRecord.app == app_name)
                .order_by(DeployActionRecord.id.desc())  # type: ignore[union-attr]
            ).all()
        )
    rows = rows[:limit]

    if not rows:
        console.print(
            Panel.fit(
                f"No DeployAction rows for [bold]{app_name}[/bold].",
                title="deploys",
            )
        )
        return

    table = Table(title=f"deploys — app={app_name} (latest {len(rows)})")
    table.add_column("id", justify="right")
    table.add_column("ts")
    table.add_column("sha")
    table.add_column("status")
    table.add_column("smoke")
    table.add_column("rb")
    table.add_column("err")
    for r in rows:
        status_color = {
            "deployed": "green",
            "rolled_back": "yellow",
            "errored": "red",
            "skipped": "blue",
        }.get(r.status, "white")
        table.add_row(
            str(r.id),
            r.ts[:19],
            r.sha[:12],
            f"[{status_color}]{r.status}[/{status_color}]",
            "yes" if r.smoke_passed else "no",
            "yes" if r.rollback_triggered else "no",
            (r.error or "")[:40],
        )
    console.print(table)


@app.command("deploy-status")
def deploy_status_cmd(
    deploy_id: int = typer.Argument(..., help="DeployActionRecord id (see `factory deploys`)"),
) -> None:
    """Show the full per-step record for a single deploy action."""
    import json as _json

    from sqlmodel import Session, select

    from factory.deploy.models import DeployActionRecord
    from factory.deploy.orchestrator import _engine as _deploy_engine

    db = _FACTORY_ROOT / "state" / "factory.db"
    eng = _deploy_engine(db)
    with Session(eng) as session:
        row = session.exec(
            select(DeployActionRecord).where(DeployActionRecord.id == deploy_id)
        ).first()
    if row is None:
        console.print(f"[red]error:[/red] no deploy action with id={deploy_id}")
        raise typer.Exit(code=2)

    console.print(
        Panel.fit(
            f"[bold]app[/bold]={row.app}  [bold]sha[/bold]={row.sha}  "
            f"[bold]status[/bold]={row.status}\n"
            f"[bold]ts[/bold]={row.ts}\n"
            f"pre_deploy_duration_s={row.pre_deploy_duration_s}  "
            f"deploy_duration_s={row.deploy_duration_s}\n"
            f"health_check_passed={row.health_check_passed}  "
            f"smoke_passed={row.smoke_passed}\n"
            f"rollback_triggered={row.rollback_triggered}  "
            f"rollback_passed={row.rollback_passed}\n"
            f"error={row.error}\n"
            f"skipped_reason={row.skipped_reason}",
            title=f"deploy #{deploy_id}",
        )
    )

    steps_table = Table(title="per-phase step results")
    steps_table.add_column("#", justify="right")
    steps_table.add_column("phase")
    steps_table.add_column("exit")
    steps_table.add_column("attempts")
    steps_table.add_column("dur_s", justify="right")
    steps_table.add_column("command")
    try:
        steps = _json.loads(row.per_phase_results_json or "[]")
    except _json.JSONDecodeError:
        steps = []
    for i, s in enumerate(steps, start=1):
        exit_code = s.get("exit_code")
        color = "green" if exit_code == 0 else "red"
        steps_table.add_row(
            str(i),
            str(s.get("phase", "")),
            f"[{color}]{exit_code}[/{color}]",
            str(s.get("attempts", 1)),
            f"{float(s.get('duration_seconds') or 0.0):.2f}",
            str(s.get("command", ""))[:80],
        )
    console.print(steps_table)


@app.command("test-slop")
def test_slop_cmd(
    path: Path = typer.Option(..., "--file", exists=True, help="Path to file or directory to scan"),
) -> None:
    """Scan a file (or directory) for slop anti-patterns.

    Surfaces every finding produced by the same ``slop_detector`` the
    auto-merge ``tests-meaningful`` gate uses. Exits non-zero if any
    findings are reported — useful as a pre-commit guard.
    """
    from factory.chain.slop_detector import scan_diff, scan_file

    findings: list[Any] = []
    if path.is_file():
        findings = scan_file(path)
    elif path.is_dir():
        # Collect every plausible test file recursively.
        candidates = [
            p
            for p in path.rglob("*")
            if p.is_file() and p.suffix in {".py", ".ts", ".tsx", ".js", ".jsx"}
        ]
        findings = scan_diff([str(c) for c in candidates])

    if not findings:
        console.print(Panel.fit(f"[green]No slop found in {path}[/green]", title="test-slop"))
        return

    table = Table(title=f"slop findings in {path}")
    table.add_column("path")
    table.add_column("line")
    table.add_column("kind")
    table.add_column("why")
    for f in findings:
        table.add_row(f.path, str(f.line), f.kind, f.why_slop[:80])
    console.print(table)
    console.print(f"[red]{len(findings)} finding(s)[/red]")
    raise typer.Exit(code=1)


def _scheduled_persona_now(
    *,
    persona: str,
    app_name: str,
    dry_run: bool,
    label: str,
) -> None:
    """Shared body for the four ``factory <persona>-now`` commands.

    Loads env, validates that real-run runs have an API key available,
    then dispatches via ``run_scheduled_persona`` and prints a summary.
    """
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)
    if not dry_run:
        ok, hint = _has_any_llm_provider_key()
        if not ok:
            console.print(f"[red]error:[/red] real {label} requires an LLM provider key. " + hint)
            raise typer.Exit(code=2)
    from factory.chain.scheduled_tasks import run_scheduled_persona

    out = run_scheduled_persona(
        persona,
        app_name,
        _FACTORY_ROOT,
        dry_run=dry_run,
    )
    table = Table(title=f"{label} — app={app_name} dry_run={dry_run}")
    table.add_column("field")
    table.add_column("value")
    table.add_row("status", out.status)
    table.add_row("findings", str(out.findings_count))
    table.add_row("directions_filed", ", ".join(out.directions_filed) or "(none)")
    table.add_row("duration_s", f"{out.duration_s:.3f}")
    if out.error:
        table.add_row("error", out.error)
    console.print(table)
    if out.status == "errored":
        raise typer.Exit(code=1)


@app.command("ralph-now")
def ralph_now_cmd(
    app_name: str = typer.Option(..., "--app", help="App name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="No LLM/GitHub/repo writes"),
) -> None:
    """Force-fire the Ralph (continuous-improvement) persona once."""
    _scheduled_persona_now(persona="ralph", app_name=app_name, dry_run=dry_run, label="ralph")


@app.command("improve")
def improve_cmd(
    app_name: str | None = typer.Option(
        None,
        "--app",
        help=(
            "Optional app filter — only blocked stories under this app "
            "are surfaced to the improver. Events are always aggregated "
            "across all apps."
        ),
    ),
    window_hours: int = typer.Option(
        24,
        "--window-hours",
        help="How far back to aggregate factory_needs_redesign events.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="No LLM/GitHub call"),
    no_issue: bool = typer.Option(
        False,
        "--no-issue",
        help=(
            "Skip the pinned-issue update. The JSON output is still "
            "persisted under state/improvements/."
        ),
    ),
    apply_pass: bool = typer.Option(
        True,
        "--apply/--no-apply",
        help=(
            "L2 apply pass: classify each proposal, apply safe ones to "
            "a fresh branch on this repo, run the factory test suite, "
            "and open PRs (auto-merging safe ones via `gh pr merge --squash --auto`). "
            "`--no-apply` skips the pass for dry-running the persona only."
        ),
    ),
    apply_repo: str = typer.Option(
        "xvanov/software-factory",
        "--apply-repo",
        help=(
            "owner/name of the factory repo where apply-pass PRs land. "
            "Pass an empty string to skip PR creation entirely "
            "(branches still get pushed locally if push is reachable)."
        ),
    ),
) -> None:
    """Run the factory_improver persona over the recent
    ``factory_needs_redesign`` events.

    Aggregates the last N hours of redesign events + terminally-blocked
    story rows, invokes the improver persona, persists the proposal to
    ``state/improvements/<ts>.json``, (unless ``--no-issue``) posts
    a summary on the rolling ``factory-improvements`` GH issue, and
    (unless ``--no-apply``) runs the L2 apply pass to open PRs against
    ``--apply-repo``.
    """
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)
    if not dry_run:
        ok, hint = _has_any_llm_provider_key()
        if not ok:
            console.print(
                "[red]error:[/red] real `factory improve` requires an LLM provider key. " + hint
            )
            raise typer.Exit(code=2)

    from factory.chain.factory_improver import run_factory_improver

    # Resolve the repo for the pinned issue: prefer the explicit app's
    # config (when provided) so the issue lands in the right place. When
    # no app filter is set, default to the factory's own repo (recorded
    # under apps/software-factory if available; otherwise skip the issue
    # post). This is the documented "factory self-improvement" path.
    repo_for_issue: str | None = None
    if not no_issue and app_name:
        try:
            from factory.app_config import load_app_config

            cfg = load_app_config(app_name, _FACTORY_ROOT)
            repo_for_issue = cfg.repo
        except Exception:
            repo_for_issue = None

    out = run_factory_improver(
        app=app_name,
        software_factory_root=_FACTORY_ROOT,
        window_hours=window_hours,
        dry_run=dry_run,
        repo_for_issue=None if no_issue else repo_for_issue,
        apply_pass=apply_pass,
        apply_repo=(apply_repo or None) if apply_pass else None,
    )

    table = Table(title=f"factory improve — app={app_name or '(all)'} dry_run={dry_run}")
    table.add_column("field")
    table.add_column("value")
    table.add_row("timestamp", out.timestamp)
    table.add_row("events_processed", str(out.events_processed))
    table.add_row("improvements", str(out.improvements_count))
    table.add_row("output_path", str(out.output_path or "(none)"))
    table.add_row("issue_number", str(out.issue_number) if out.issue_number else "(none)")
    if out.apply_summary is not None:
        s = out.apply_summary
        table.add_row("applied (safe)", str(s.applied))
        table.add_row("queued_for_review (risky)", str(s.queued_for_review))
        table.add_row("abandoned", str(s.abandoned))
        table.add_row("invalid", str(s.invalid))
    if out.error:
        table.add_row("error", out.error)
    console.print(table)
    if out.error:
        raise typer.Exit(code=1)


@app.command("bug-hunt-now")
def bug_hunt_now_cmd(
    app_name: str = typer.Option(..., "--app", help="App name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="No LLM/GitHub/repo writes"),
) -> None:
    """Force-fire the Bug-Hunter persona once."""
    _scheduled_persona_now(
        persona="bug_hunter", app_name=app_name, dry_run=dry_run, label="bug-hunt"
    )


@app.command("ux-audit-now")
def ux_audit_now_cmd(
    app_name: str = typer.Option(..., "--app", help="App name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="No LLM/GitHub/repo writes"),
) -> None:
    """Force-fire the UX-Auditor persona once."""
    _scheduled_persona_now(
        persona="ux_auditor", app_name=app_name, dry_run=dry_run, label="ux-audit"
    )


@app.command("security-now")
def security_now_cmd(
    app_name: str = typer.Option(..., "--app", help="App name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="No LLM/GitHub/repo writes"),
) -> None:
    """Force-fire the Security persona once."""
    _scheduled_persona_now(persona="security", app_name=app_name, dry_run=dry_run, label="security")


@app.command("security-scan")
def security_scan_cmd(
    app_name: str = typer.Option(..., "--app", help="App name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="No LLM/GitHub/repo writes"),
) -> None:
    """Alias for ``security-now`` (matches the Phase 6 spec naming)."""
    _scheduled_persona_now(
        persona="security", app_name=app_name, dry_run=dry_run, label="security-scan"
    )


@app.command("schedules")
def schedules_cmd() -> None:
    """List cron schedules with last-run and next-fire times.

    Reads from ``factory_settings.yaml`` (or built-in defaults) and the
    ``cron_schedules`` table for last-run timestamps. Pure read; no
    dispatch happens.
    """
    from factory.scheduler.cron import (
        get_schedule_row,
        load_schedules,
        next_fire,
    )

    schedules = load_schedules(_FACTORY_ROOT)
    db = _FACTORY_ROOT / "state" / "factory.db"
    table = Table(title="schedules")
    table.add_column("name")
    table.add_column("persona")
    table.add_column("cron")
    table.add_column("last_run")
    table.add_column("last_status")
    table.add_column("next_fire (UTC)")
    for s in schedules:
        row = get_schedule_row(s.name, db)
        last_run = (row.last_run if row else None) or "(never)"
        last_status = (row.last_status if row else None) or "-"
        try:
            nxt = next_fire(s).isoformat()
        except Exception as exc:  # noqa: BLE001
            nxt = f"[invalid: {exc}]"
        table.add_row(s.name, s.persona, s.cron_expr, last_run, last_status, nxt)
    console.print(table)


# --------------------------------------------------------------------------- #
# Phase-7 commands: status-sync, idle-check.
# --------------------------------------------------------------------------- #


@app.command("status-sync")
def status_sync_cmd(
    app_name: str = typer.Option(..., "--app", help="App name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="No GitHub calls; print body only"),
) -> None:
    """Update the pinned ``factory-status`` GitHub issue for ``--app``.

    Recommended cron: every 5 minutes. In dry-run, no GH API call is
    made; the composed body is printed for inspection.
    """
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)

    from factory.chain.factory_status import compose_status_body, update_status_issue

    body = compose_status_body(app_name, _FACTORY_ROOT)
    if dry_run:
        console.print(Panel(body, title=f"factory-status (dry-run) — app={app_name}"))
        return

    gh = _ensure_github_client()
    number = update_status_issue(
        app=app_name,
        software_factory_root=_FACTORY_ROOT,
        github_client=gh,
    )
    console.print(
        Panel.fit(
            f"Updated [bold]factory-status[/bold] issue #{number} for app=[bold]{app_name}[/bold]",
            title="status-sync",
            style="green",
        )
    )


@app.command("idle-check")
def idle_check_cmd(
    app_name: str = typer.Option(..., "--app", help="App name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="No GitHub calls; print snapshot only"),
    since_hours: int = typer.Option(2, "--since-hours", help="Idle threshold window in hours"),
) -> None:
    """Detect whether ``--app`` has gone idle and open/update the ``factory-idle`` issue.

    Idle = queue empty AND no in-flight stories AND no scheduled
    persona findings in the last ``--since-hours`` hours AND no recent
    deploys. Recommended cron: every 30 minutes.
    """
    load_dotenv()
    load_dotenv(_FACTORY_ROOT / ".env", override=False)

    from factory.chain.idle import detect_idle, open_idle_issue

    snapshot = detect_idle(app_name, _FACTORY_ROOT, since_hours=since_hours)
    if snapshot is None:
        console.print(
            Panel.fit(
                f"App [bold]{app_name}[/bold] is not idle "
                f"(work in flight, or activity within {since_hours}h).",
                title="idle-check",
            )
        )
        return

    recent_lines = (
        "\n".join(f"- `{d.id}-{d.slug}` ({d.title})" for d in snapshot.recent_directions)
        or "_(no recent directions)_"
    )
    body_preview = (
        f"Idle since: `{snapshot.idle_since.isoformat()}`\n\n"
        f"Recent directions (most recent first):\n{recent_lines}"
    )

    if dry_run:
        console.print(
            Panel(
                body_preview,
                title=f"factory-idle (dry-run) — app={app_name}",
                style="yellow",
            )
        )
        return

    gh = _ensure_github_client()
    number = open_idle_issue(snapshot, gh, software_factory_root=_FACTORY_ROOT)
    console.print(
        Panel.fit(
            f"Updated [bold]factory-idle[/bold] issue #{number} for app=[bold]{app_name}[/bold]",
            title="idle-check",
            style="green",
        )
    )


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


# --------------------------------------------------------------------------- #
# Phase FMS-1 commands: manager (signal inspection)
# --------------------------------------------------------------------------- #


manager_app = typer.Typer(help="FMS manager sub-commands.")
app.add_typer(manager_app, name="manager")

signals_app = typer.Typer(help="Inspect structured event streams.")
manager_app.add_typer(signals_app, name="signals")


def _parse_duration(s: str) -> float:
    """Parse a simple duration string into seconds.

    Accepted formats: ``30s``, ``15m``, ``2h``, ``1d``.
    Falls back to treating the raw value as seconds if no suffix.
    """
    s = s.strip().lower()
    if s.endswith("d"):
        return float(s[:-1]) * 86400
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("s"):
        return float(s[:-1])
    return float(s)


@signals_app.command("dump")
def signals_dump_cmd(
    since: str = typer.Option("1h", "--since", help="Duration to look back (e.g. 1h, 30m, 2d)"),
    stream: str | None = typer.Option(
        None, "--stream", help="Only show events from this stream (e.g. ticks, runs)"
    ),
    fmt: str = typer.Option("human", "--format", help="Output format: human | json"),
) -> None:
    """Print all events from the signal streams since ``--since``, interleaved by ts."""
    import json as _json
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from factory.manager.signals import _events_dir

    events_dir = _events_dir(_FACTORY_ROOT)
    if not events_dir.exists():
        console.print(
            Panel.fit(
                f"No events directory at [bold]{events_dir}[/bold]. "
                "Run [bold]factory tick --app <app> --dry-run[/bold] first.",
                title="signals dump",
            )
        )
        return

    try:
        window_s = _parse_duration(since)
    except ValueError:
        console.print(f"[red]error:[/red] could not parse --since={since!r}")
        raise typer.Exit(code=2) from None

    cutoff = _dt.now(_UTC) - _td(seconds=window_s)

    # Collect all .ndjson files in the events directory.
    stream_files = sorted(events_dir.glob("*.ndjson"))
    if stream:
        stream_files = [f for f in stream_files if f.stem == stream]
    if not stream_files:
        console.print(f"[dim]No event streams found (stream={stream!r}).[/dim]")
        return

    all_events: list[tuple[str, str, dict[str, Any]]] = []  # (ts_str, stream_name, record)
    for sf in stream_files:
        try:
            lines = sf.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            ts_str = rec.get("ts") or ""
            try:
                ts_dt = _dt.fromisoformat(ts_str)
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=_UTC)
            except (TypeError, ValueError):
                continue
            if ts_dt < cutoff:
                continue
            all_events.append((ts_str, sf.stem, rec))

    if not all_events:
        console.print(f"[dim]No events in the last {since}.[/dim]")
        return

    # Sort by ts ascending.
    all_events.sort(key=lambda x: x[0])

    for ts_str, stream_name, rec in all_events:
        if fmt == "json":
            # Use plain print so Rich doesn't wrap or markup the JSON.
            print(_json.dumps(rec))
        else:
            event = rec.get("event", "?")
            ts_short = ts_str[:19].replace("T", " ") if ts_str else "?"
            # Build a concise summary from the most useful fields.
            highlights: list[str] = []
            for key in (
                "story_id",
                "persona",
                "success",
                "duration_s",
                "app",
                "kind",
                "result",
                "tick_id",
                "pr_number",
            ):
                if rec.get(key) is not None:
                    highlights.append(f"{key}={rec[key]!r}")
            summary = " ".join(highlights) if highlights else ""
            console.print(f"[{ts_short}] {stream_name}/{event} {summary}")


# --------------------------------------------------------------------------- #
# Phase FMS-3 commands: manager watch (L1 Watcher agent)
# --------------------------------------------------------------------------- #


@manager_app.command("watch")
def manager_watch_cmd(
    once: bool = typer.Option(False, "--once", help="Run a single watcher cycle and exit."),
    interval_s: int = typer.Option(60, "--interval-s", help="Seconds between watcher cycles (daemon mode)."),
    max_iters: int | None = typer.Option(None, "--max-iters", help="Stop after N iterations (useful for testing)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Assemble prompt but do NOT call the LLM; print the prompt."),
    no_l2: bool = typer.Option(False, "--no-l2", help="Suppress the immediate L2 trigger on L1 escalation (useful for testing L1 in isolation)."),
    no_l3: bool = typer.Option(False, "--no-l3", help="Suppress the immediate L3 trigger on L2 escalation (useful for testing L2 in isolation)."),
) -> None:
    """Run the L1 Watcher agent.

    Without ``--once``, runs as a daemon looping every ``--interval-s``
    seconds until SIGINT.  With ``--once``, runs a single cycle and
    prints the result JSON.  With ``--once --dry-run``, assembles and
    prints the prompt without calling the LLM.

    In daemon mode, when L1 escalates (``escalate_to_l2=true``), an
    immediate L2 summarizer iteration is triggered unless ``--no-l2``
    is passed.  When L2 escalates (``escalate_to_l3=true``), an immediate
    L3 diagnostician iteration is triggered unless ``--no-l3`` is passed.
    """
    from factory.manager.watcher import run_watcher_daemon, run_watcher_once

    if once:
        result = run_watcher_once(root=_FACTORY_ROOT, dry_run=dry_run)
        if not dry_run:
            # Pretty-print the result envelope.
            import json as _json
            print(_json.dumps(result, indent=2, default=str))
    else:
        if dry_run:
            console.print(
                "[yellow]--dry-run has no effect in daemon mode; use --once --dry-run.[/yellow]"
            )
        run_watcher_daemon(
            root=_FACTORY_ROOT,
            interval_s=interval_s,
            max_iters=max_iters,
            trigger_l2=not no_l2,
            trigger_l3=not no_l3,
        )


@manager_app.command("summarize")
def manager_summarize_cmd(
    once: bool = typer.Option(False, "--once", help="Run a single summarizer cycle and exit."),
    interval_s: int = typer.Option(
        180, "--interval-s", help="Seconds between summarizer cycles (daemon mode). Default: 180."
    ),
    max_iters: int | None = typer.Option(
        None, "--max-iters", help="Stop after N iterations (useful for testing)."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Assemble L2 prompt but do NOT call the LLM; print the prompt.",
    ),
) -> None:
    """Run the L2 Summarizer agent.

    Reads watcher notes flagged with ``escalate_to_l2=true`` and produces
    structured concern documents under ``state/concerns/``.

    Without ``--once``, runs as a daemon looping every ``--interval-s``
    seconds (default: 180) until SIGINT.  With ``--once``, runs a single
    cycle and prints the resulting concern JSON (or reports that no flagged
    notes were found).  With ``--once --dry-run``, assembles and prints
    the L2 prompt without calling the LLM.
    """
    from factory.manager.summarizer import run_summarizer_daemon, run_summarizer_once

    if once:
        result = run_summarizer_once(root=_FACTORY_ROOT, dry_run=dry_run)
        if result is None:
            console.print("[dim]No flagged watcher notes found — nothing to summarize.[/dim]")
        elif not dry_run:
            import json as _json
            print(_json.dumps(result, indent=2, default=str))
    else:
        if dry_run:
            console.print(
                "[yellow]--dry-run has no effect in daemon mode; use --once --dry-run.[/yellow]"
            )
        run_summarizer_daemon(
            root=_FACTORY_ROOT,
            interval_s=interval_s,
            max_iters=max_iters,
        )


# --------------------------------------------------------------------------- #
# Phase FMS-5 commands: manager diagnose (L3 Diagnostician agent)
# --------------------------------------------------------------------------- #


@manager_app.command("diagnose")
def manager_diagnose_cmd(
    once: bool = typer.Option(False, "--once", help="Run a single diagnostician cycle and exit."),
    interval_s: int = typer.Option(
        300, "--interval-s", help="Seconds between diagnostician cycles (daemon mode). Default: 300."
    ),
    max_iters: int | None = typer.Option(
        None, "--max-iters", help="Stop after N iterations (useful for testing)."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Assemble L3 prompt but do NOT call the LLM; print the prompt.",
    ),
    concern: str | None = typer.Option(
        None, "--concern", help="Path to a specific concern JSON file to diagnose."
    ),
) -> None:
    """Run the L3 Diagnostician agent.

    Reads unprocessed concern documents from ``state/concerns/`` and
    produces structured proposals under ``state/manager_proposals/``.

    Without ``--once``, runs as a daemon looping every ``--interval-s``
    seconds (default: 300) until SIGINT.  With ``--once``, runs a single
    cycle and prints the resulting proposal JSON (or reports that no
    unprocessed concerns were found).  With ``--once --dry-run``, assembles
    and prints the L3 prompt without calling the LLM.

    Use ``--concern <path>`` to diagnose a specific concern file instead of
    picking the most-recent unprocessed one.
    """
    from factory.manager.diagnostician import run_diagnostician_daemon, run_diagnostician_once

    concern_path = Path(concern) if concern else None

    if once:
        result = run_diagnostician_once(
            root=_FACTORY_ROOT, concern_path=concern_path, dry_run=dry_run
        )
        if result is None:
            console.print("[dim]No unprocessed concerns found — nothing to diagnose.[/dim]")
        elif not dry_run:
            import json as _json
            print(_json.dumps(result, indent=2, default=str))
    else:
        if dry_run:
            console.print(
                "[yellow]--dry-run has no effect in daemon mode; use --once --dry-run.[/yellow]"
            )
        run_diagnostician_daemon(
            root=_FACTORY_ROOT,
            interval_s=interval_s,
            max_iters=max_iters,
        )
