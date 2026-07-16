# Factory vs Claude Code — long-horizon benchmark

Answers: **does the improved factory (open models, no Anthropic) match plain
Claude Code on real backlog tasks, at a fraction of the cost?**

Historical baseline (the "before" picture, from `state/factory.db`): ~0.68
merged stories/day, mean $17/shipped story, median ~17 calendar days/story.

## Arms

| arm | what runs | cost accounting |
|---|---|---|
| `factory` | improved chain (OpenRouter/Azure open models, dev convergence ON) in an **isolated bench root** — own state db/settings/worktrees; production scheduler untouched | `sum(runs.cost_usd)` in the bench db |
| `claude` | one-shot `claude -p` (subscription) in a worktree, same prompt text | `total_cost_usd` from the CLI JSON |

Both arms get the same frozen `base_sha`, the same prompt (the real
direction/story markdown), and the same done-oracle: sacrifice's own gate
commands run via the factory's `_isolated_test_env`, plus a blind LLM-judge
rubric (judge never sees which arm made the diff).

## Protocol

1. Freeze the campaign: set `base_sha` in `tasks.yaml`.
2. Per task (see `tasks.yaml`; N=2 for t1–t7, N=1 for the epic t8):
   ```bash
   uv run python bench/bench.py run-claude  --task <id> --run <n>
   uv run python bench/bench.py run-factory --task <id> --run <n>
   uv run python bench/bench.py gate   --task <id> --arm <arm> --run <n>
   uv run python bench/bench.py rubric --task <id> --arm <arm> --run <n>
   ```
3. `uv run python bench/bench.py report` → `bench/results/summary.md`.

## Preconditions

- Azure credentials in `.env` (the factory arm runs entirely on the
  mv-coding-agent-foundry deployments: gpt-5.4 / gpt-5.3-codex / deepseek-v4-pro).
- `sacrifice-db` container startable (`make -C ../sacrifice up-db`) — the
  smoke gate boots isolated backends against it.
- Claude subscription authed on this machine (`claude -p` works).
- Don't run two arms of the same task concurrently (they share uv/docker).

## Success definition

Parity: factory gates-pass rate ≥ claude on t1–t7 at ≤ 1/5 the $ cost.
t8 (epic) measures the remaining long-horizon ceiling gap and is expected
to favor Claude Code.

## Known confounds (accepted + why)

- **Retries**: the factory gets its retry budget inside one invocation
  (convergence loop); claude gets one shot with in-session iteration. This is
  the design difference under test, not a bug — both are "the tool as used".
- **Judge**: gpt-5.4 is also a factory-arm persona model; mitigated by
  blinding (anonymous diffs, no arm labels). No model exists that is in
  neither arm and free of family bias in some direction.
- **Shared db container**: smoke runs of both arms use the sacrifice-db
  container with throwaway rows; journeys are register-fresh each run.
