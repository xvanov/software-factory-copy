"""Typer-based `factory` CLI. Phase-0 surface only.

Subcommands shipped in Phase 0:
  * ``factory --version``
  * ``factory hello`` — sanity check
  * ``factory test-persona dev --story <path> --repo <path> [--difficulty ...] [--dry-run]``

Later phases add: ``new-direction``, ``tell``, ``pm-sync``, ``inbox``, ``queue``,
``mode``, ``budget``, ``why``, ``ralph-now``, etc.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from factory import __version__
from factory.model_router import route
from factory.runner import LLMConfig, sandbox_run

app = typer.Typer(help="Factory CLI — orchestrate the software factory.")
test_persona_app = typer.Typer(help="Run a single persona end-to-end for testing.")
app.add_typer(test_persona_app, name="test-persona")

console = Console()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
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
            f"[bold green]factory[/bold green] v{__version__} is alive.\n"
            f"Run [bold]factory test-persona dev --help[/bold] for Phase-0 usage.",
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
    # Load .env from cwd or from ~/software-factory/.env if running from elsewhere.
    load_dotenv()
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

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
