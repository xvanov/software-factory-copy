"""Factory-vs-Claude-Code benchmark driver.

Arms
----
  factory : the improved factory chain (open models via routes.yaml), driven
            story-by-story in an ISOLATED bench root — its own state db,
            settings, and worktrees — so nothing leaks into the production
            scheduler and the production timer can keep running.
  claude  : plain `claude -p` (subscription), one shot per run, in a git
            worktree off the same frozen base SHA.

Every (task, arm, run) gets its own sacrifice worktree. Done-criteria are
identical: `gate` re-runs sacrifice's own gate commands in the run's worktree
via the factory's `_isolated_test_env`; `rubric` does a blind LLM-judge pass
over the diff vs the task's acceptance criteria.

Usage (from the factory root):
  uv run python bench/bench.py run-claude  --task t3_csrf --run 1
  uv run python bench/bench.py run-factory --task t3_csrf --run 1
  uv run python bench/bench.py gate        --task t3_csrf --arm claude --run 1
  uv run python bench/bench.py rubric      --task t3_csrf --arm claude --run 1
  uv run python bench/bench.py report

Results land as JSON under bench/runs/<task>/<arm>-<run>/result.json and are
aggregated by `report` into bench/results/summary.md.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

FACTORY_ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = FACTORY_ROOT / "bench"
RUNS_DIR = BENCH_DIR / "runs"
RESULTS_DIR = BENCH_DIR / "results"
SACRIFICE_REPO = (FACTORY_ROOT / ".." / "sacrifice").resolve()

# High fake issue numbers so bench feature branches never collide with
# production worktree branches (which are named from real issue numbers).
BENCH_ISSUE_BASE = 90000


def _load_tasks() -> dict[str, Any]:
    data = yaml.safe_load((BENCH_DIR / "tasks.yaml").read_text(encoding="utf-8"))
    return data


def _task(data: dict[str, Any], task_id: str) -> dict[str, Any]:
    for t in data["tasks"]:
        if t["id"] == task_id:
            return t
    raise SystemExit(f"unknown task {task_id!r}; known: {[t['id'] for t in data['tasks']]}")


def _base_sha(data: dict[str, Any]) -> str:
    sha = (data.get("base_sha") or "").strip()
    if sha:
        return sha
    out = subprocess.run(
        ["git", "-C", str(SACRIFICE_REPO), "rev-parse", "origin/main"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _run_dir(task_id: str, arm: str, run: int) -> Path:
    d = RUNS_DIR / task_id / f"{arm}-{run}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_worktree(task_id: str, arm: str, run: int, sha: str) -> Path:
    wt = _run_dir(task_id, arm, run) / "worktree"
    if wt.exists():
        subprocess.run(
            ["git", "-C", str(SACRIFICE_REPO), "worktree", "remove", "--force", str(wt)],
            capture_output=True, text=True,
        )
        shutil.rmtree(wt, ignore_errors=True)
    branch = f"bench/{task_id}-{arm}-{run}"
    subprocess.run(
        ["git", "-C", str(SACRIFICE_REPO), "branch", "-D", branch],
        capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(SACRIFICE_REPO), "worktree", "add", "-b", branch, str(wt), sha],
        capture_output=True, text=True, check=True,
    )
    # Replicate runtime env files the same way the factory chain does.
    for src, dst in [
        (SACRIFICE_REPO / ".env", wt / ".env"),
        (SACRIFICE_REPO / "backend" / ".env", wt / "backend" / ".env"),
    ]:
        if src.exists():
            shutil.copy2(src, dst)
    return wt


def _prompt_text(task: dict[str, Any]) -> str:
    return (FACTORY_ROOT / task["prompt_file"]).read_text(encoding="utf-8")


def _write_result(task_id: str, arm: str, run: int, payload: dict[str, Any]) -> Path:
    out = _run_dir(task_id, arm, run) / "result.json"
    existing: dict[str, Any] = {}
    if out.exists():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    existing.update(payload)
    out.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return out


def _diff_stats(wt: Path) -> dict[str, Any]:
    subprocess.run(["git", "-C", str(wt), "add", "-A"], capture_output=True)
    stat = subprocess.run(
        ["git", "-C", str(wt), "diff", "--cached", "--stat"],
        capture_output=True, text=True,
    ).stdout
    return {"diff_stat_tail": stat.strip().splitlines()[-1] if stat.strip() else "(no changes)"}


# --------------------------------------------------------------------------- #
# claude arm
# --------------------------------------------------------------------------- #

CLAUDE_PROMPT_TEMPLATE = """\
You are working in a git worktree of the `sacrifice` app (FastAPI backend in
backend/, React frontend in frontend/). Implement the following work item
end-to-end: production code AND meaningful tests. Iterate until the test
suite passes. Do not commit; leave changes in the working tree.

Definition of done:
- All acceptance criteria implemented.
- `cd backend && uv run --extra dev pytest -q tests/` passes.
- No unrelated changes.

WORK ITEM:
{prompt}
"""


def run_claude(task_id: str, run: int, *, budget_usd: float, timeout_s: int) -> None:
    data = _load_tasks()
    task = _task(data, task_id)
    sha = _base_sha(data)
    wt = _make_worktree(task_id, "claude", run, sha)
    prompt = CLAUDE_PROMPT_TEMPLATE.format(prompt=_prompt_text(task))

    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--max-budget-usd", str(budget_usd),
    ]
    started = time.monotonic()
    proc = subprocess.run(
        cmd, cwd=str(wt), capture_output=True, text=True, timeout=timeout_s,
    )
    wall_s = time.monotonic() - started
    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pass
    result = {
        "arm": "claude",
        "task": task_id,
        "run": run,
        "base_sha": sha,
        "ts": datetime.now(UTC).isoformat(),
        "wall_clock_s": round(wall_s, 1),
        "exit_code": proc.returncode,
        "cost_usd": parsed.get("total_cost_usd"),
        "num_turns": parsed.get("num_turns"),
        "is_error": parsed.get("is_error", proc.returncode != 0),
        "stderr_tail": proc.stderr[-800:],
        **_diff_stats(wt),
    }
    out = _write_result(task_id, "claude", run, result)
    print(f"claude arm done: {out}")


# --------------------------------------------------------------------------- #
# factory arm (isolated bench root — production state db is never touched)
# --------------------------------------------------------------------------- #

FACTORY_TERMINAL_HINT = """Factory arm drives dev→review only (the same
implementation surface the claude arm gets); gates run separately so both
arms share one done-oracle."""


def _build_bench_root(task_id: str, run: int, wt: Path) -> Path:
    """A minimal factory-root clone: own state db + settings, app repo -> the
    run's worktree, story/direction files copied from the real backlog."""
    root = _run_dir(task_id, "factory", run) / "root"
    if root.exists():
        shutil.rmtree(root)
    (root / "state").mkdir(parents=True)
    app_dir = root / "apps" / "sacrifice"
    (app_dir / "stories").mkdir(parents=True)
    (app_dir / "directions").mkdir(parents=True)

    # App config: same gates, but app_repo_path -> this run's worktree.
    cfg = yaml.safe_load(
        (FACTORY_ROOT / "apps" / "sacrifice" / "config.yaml").read_text(encoding="utf-8")
    )
    cfg["app_repo_path"] = str(wt)
    (app_dir / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    # Copy context dir reference if the prelude composer needs it (read-only,
    # points at the worktree's own context/ via app_repo_path).

    # Settings: production settings + dev convergence ON (the bench measures
    # the improved factory).
    settings = yaml.safe_load((FACTORY_ROOT / "factory_settings.yaml").read_text(encoding="utf-8"))
    settings.setdefault("dev_convergence", {})
    settings["dev_convergence"]["enabled"] = True
    (root / "factory_settings.yaml").write_text(
        yaml.safe_dump(settings, sort_keys=False), encoding="utf-8"
    )
    return root


def run_factory(task_id: str, run: int, *, max_steps: int, timeout_s: int) -> None:
    data = _load_tasks()
    task = _task(data, task_id)
    sha = _base_sha(data)
    wt = _make_worktree(task_id, "factory", run, sha)
    root = _build_bench_root(task_id, run, wt)
    db = root / "state" / "factory.db"

    # Seed the story at SM_DONE with the task prompt as the story file — the
    # same text the claude arm receives.
    story_rel = f"stories/{BENCH_ISSUE_BASE + run}-{task_id}.md"
    (root / "apps" / "sacrifice" / story_rel).write_text(
        _prompt_text(task), encoding="utf-8"
    )

    sys.path.insert(0, str(FACTORY_ROOT))
    from sqlmodel import Session

    from factory.app_config import load_app_config
    from factory.chain import orchestrator as O  # noqa: N812
    from factory.chain.state_machine import StoryRecord, StoryState
    from factory.runner import _engine

    story = StoryRecord(
        id=None,
        direction_id="bench",
        app="sacrifice",
        title=task_id,
        slug=f"bench-{task_id}-{run}",
        scope=task["scope"],
        state=StoryState.SM_DONE.value,
        github_issue_number=BENCH_ISSUE_BASE + run,
        story_file_path=story_rel,
    )
    eng = _engine(db)
    with Session(eng) as s:
        s.add(story)
        s.commit()
        s.refresh(story)
    story_id = story.id

    cfg = load_app_config("sacrifice", root)
    allowed = {"dev", "review"}
    terminal = {
        StoryState.REVIEWER_DONE.value,
        StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value,
        StoryState.BLOCKED_REVIEW_NONCONVERGENT.value,
    }
    started = time.monotonic()
    transitions: list[str] = []
    error: str | None = None
    for _ in range(max_steps):
        if time.monotonic() - started > timeout_s:
            error = f"bench wall-clock cap {timeout_s}s hit"
            break
        with Session(eng) as s:
            row = s.get(StoryRecord, story_id)
            assert row is not None
        if row.state in terminal:
            break
        name = O._dispatch_for_story(row)
        if name not in allowed:
            transitions.append(f"stop: state={row.state} dispatches {name}")
            break
        before = row.state
        try:
            O._invoke_handler(name, row, cfg, root, dry_run=False, db_path=db)
        except Exception as exc:  # record and stop — never crash the driver
            error = f"{name}: {type(exc).__name__}: {exc}"
            break
        transitions.append(f"{name}: {before} -> {row.state}")
        print(transitions[-1], flush=True)

    with Session(eng) as s:
        final = s.get(StoryRecord, story_id)
        assert final is not None
        from sqlmodel import select

        from factory.runner import Run

        runs = s.exec(select(Run).where(Run.story_id == story_id)).all()
        cost = sum(float(r.cost_usd or 0.0) for r in runs)

    # The chain does its work in its OWN per-story worktree under the bench
    # root — that tree (not the seed worktree) holds the diff to grade. The
    # worktree dir is named from the GitHub issue number, not the db id.
    candidates = sorted((root / "state" / "worktrees").glob("sacrifice-*"))
    graded_wt = candidates[0] if candidates else wt

    result = {
        "arm": "factory",
        "task": task_id,
        "run": run,
        "base_sha": sha,
        "ts": datetime.now(UTC).isoformat(),
        "wall_clock_s": round(time.monotonic() - started, 1),
        "final_state": final.state,
        "dev_retries": final.dev_retries,
        "reviewer_cycles": final.reviewer_cycles,
        "cost_usd": round(cost, 4),
        "persona_calls": len(runs),
        "transitions": transitions[-12:],
        "error": error,
        "worktree_path": str(graded_wt),
        **_diff_stats(graded_wt),
    }
    out = _write_result(task_id, "factory", run, result)
    print(f"factory arm done: {out}")


# --------------------------------------------------------------------------- #
# gates + rubric + report
# --------------------------------------------------------------------------- #


def _graded_worktree(task_id: str, arm: str, run: int) -> Path:
    """The tree holding the arm's diff: the factory arm records its per-story
    chain worktree in result.json; the claude arm uses the seed worktree."""
    result_file = _run_dir(task_id, arm, run) / "result.json"
    if result_file.exists():
        try:
            recorded = json.loads(result_file.read_text(encoding="utf-8")).get("worktree_path")
            if recorded and Path(recorded).exists():
                return Path(recorded)
        except json.JSONDecodeError:
            pass
    return _run_dir(task_id, arm, run) / "worktree"


def gate(task_id: str, arm: str, run: int) -> None:
    data = _load_tasks()
    task = _task(data, task_id)
    wt = _graded_worktree(task_id, arm, run)
    if not wt.exists():
        raise SystemExit(f"no worktree at {wt} — run the arm first")

    sys.path.insert(0, str(FACTORY_ROOT))
    from factory.app_config import load_app_config
    from factory.runner import _isolated_test_env

    cfg = load_app_config("sacrifice", FACTORY_ROOT)
    labels = data["gates"].get(task["scope"], [])
    env = _isolated_test_env()
    gates_out: dict[str, Any] = {}
    for label in labels:
        cmd = getattr(cfg.gates, label, None)
        if not cmd:
            gates_out[label] = {"skipped": "no command configured"}
            continue
        proc = subprocess.run(
            cmd, shell=True, cwd=str(wt), env=env,
            capture_output=True, text=True, timeout=1200,
        )
        gates_out[label] = {
            "exit_code": proc.returncode,
            "tail": (proc.stdout + proc.stderr)[-600:],
        }
        print(f"[{label}] exit={proc.returncode}")
    passed = all(
        (g.get("exit_code") == 0) for g in gates_out.values() if "exit_code" in g
    )
    _write_result(task_id, arm, run, {"gates": gates_out, "gates_passed": passed})
    print(f"gates_passed={passed}")


RUBRIC_PROMPT = """\
You are grading an anonymous code diff against a work item's acceptance
criteria. You do NOT know what tool produced the diff — grade only what you
see. Return STRICT JSON:
{{"ac_coverage": 0.0-1.0, "scope_discipline": 0.0-1.0, "test_quality": 0.0-1.0,
 "readability": 0.0-1.0, "overall": 0.0-1.0, "pass": true/false,
 "notes": "<3 sentences max>"}}

WORK ITEM:
{prompt}

DIFF (may be truncated):
{diff}
"""


def rubric(task_id: str, arm: str, run: int) -> None:
    data = _load_tasks()
    task = _task(data, task_id)
    wt = _graded_worktree(task_id, arm, run)
    # Uncommitted (claude arm) → staged → committed-vs-base (factory arm
    # commits its work in the chain worktree).
    diff = subprocess.run(
        ["git", "-C", str(wt), "diff", "--cached"], capture_output=True, text=True
    ).stdout
    if not diff.strip():
        diff = subprocess.run(
            ["git", "-C", str(wt), "diff"], capture_output=True, text=True
        ).stdout
    if not diff.strip():
        base = json.loads(
            ((_run_dir(task_id, arm, run) / "result.json").read_text(encoding="utf-8"))
        ).get("base_sha", "origin/main")
        diff = subprocess.run(
            ["git", "-C", str(wt), "diff", f"{base}...HEAD"], capture_output=True, text=True
        ).stdout

    sys.path.insert(0, str(FACTORY_ROOT))
    from factory.runner import text_run

    prompt = RUBRIC_PROMPT.format(prompt=_prompt_text(task), diff=diff[:60000])
    schema = {
        "type": "object",
        "required": ["ac_coverage", "scope_discipline", "test_quality",
                     "readability", "overall", "pass", "notes"],
    }
    res = text_run(
        persona="bench_rubric_judge",
        prompt=prompt,
        model_id="azure/gpt-5.4",
        schema=schema,
        db_path=_run_dir(task_id, arm, run) / "rubric.db",
    )
    _write_result(task_id, arm, run, {"rubric": res})
    print(json.dumps(res, indent=2))


def report() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for result_file in sorted(RUNS_DIR.glob("*/*/result.json")):
        try:
            rows.append(json.loads(result_file.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    lines = [
        "# Factory vs Claude Code — benchmark results",
        "",
        f"Generated {datetime.now(UTC).isoformat()}. "
        "Success = gates green (rubric shown for diff quality).",
        "",
        "| task | arm | run | gates | rubric overall | wall clock | cost $ | notes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        rub = (r.get("rubric") or {})
        lines.append(
            f"| {r.get('task')} | {r.get('arm')} | {r.get('run')} "
            f"| {'PASS' if r.get('gates_passed') else ('—' if 'gates_passed' not in r else 'FAIL')} "
            f"| {rub.get('overall', '—')} "
            f"| {r.get('wall_clock_s', '—')}s "
            f"| {r.get('cost_usd', '—')} "
            f"| {r.get('final_state') or r.get('diff_stat_tail', '')} |"
        )
    out = RESULTS_DIR / "summary.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out)
    print("\n".join(lines))


def main() -> None:
    # The factory CLI loads .env at every entry point; the bench driver calls
    # runner/handler internals directly, so it must do the same.
    from dotenv import load_dotenv

    load_dotenv(FACTORY_ROOT / ".env", override=False)

    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run-claude")
    p.add_argument("--task", required=True)
    p.add_argument("--run", type=int, default=1)
    p.add_argument("--budget-usd", type=float, default=10.0)
    p.add_argument("--timeout-s", type=int, default=3600)

    p = sub.add_parser("run-factory")
    p.add_argument("--task", required=True)
    p.add_argument("--run", type=int, default=1)
    p.add_argument("--max-steps", type=int, default=16)
    p.add_argument("--timeout-s", type=int, default=7200)

    for name in ("gate", "rubric"):
        p = sub.add_parser(name)
        p.add_argument("--task", required=True)
        p.add_argument("--arm", required=True, choices=["factory", "claude"])
        p.add_argument("--run", type=int, default=1)

    sub.add_parser("report")

    args = ap.parse_args()
    if args.cmd == "run-claude":
        run_claude(args.task, args.run, budget_usd=args.budget_usd, timeout_s=args.timeout_s)
    elif args.cmd == "run-factory":
        run_factory(args.task, args.run, max_steps=args.max_steps, timeout_s=args.timeout_s)
    elif args.cmd == "gate":
        gate(args.task, args.arm, args.run)
    elif args.cmd == "rubric":
        rubric(args.task, args.arm, args.run)
    elif args.cmd == "report":
        report()


if __name__ == "__main__":
    main()
