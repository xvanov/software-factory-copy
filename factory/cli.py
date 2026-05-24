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
