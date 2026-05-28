# Personas — contracts, model tiers, and breakpoints

## Overview

The `factory/personas/*.md` files define the factory’s agent contracts: what each persona receives, what it is allowed to do, what artifact it must emit, and what downstream stage consumes that artifact. They are not generic role descriptions; they are load-bearing protocol specs that drive routing, output validation, and chain control flow. Model tiering is configured centrally in `factory/routes.yaml`, with most structured-text personas on `azure/gpt-5.4`, implementation personas on `azure/deepseek-v4-pro`, and a few direct-provider routes defined as fallbacks when `default_provider` is not Azure.

## Key concepts

- **Two execution modes:** some personas are **text-only JSON producers** (`pm`, `sm`, `analyst`, `architect`, `reviewer`, etc.); others must **side-effect the worktree via file-edit / bash tools** (`dev`, `test_implementer`, `onboarder`).
- **Canonical handoff chain:** `pm` decomposes a `Direction`; `sm` writes BMAD story files; optional `ux_designer` and `architect` augment context; `test_designer` plans tests; `test_implementer` writes red tests; `dev` writes production code; `reviewer` approves or requests changes; `tech_writer` rewrites context.
- **Scheduled personas:** `bug_hunter`, `ralph`, `security`, `ux_auditor`, `factory_improver`, and the manager stack (`manager_watcher`, `manager_summarizer`, `manager_diagnostician`) run from cron or manager loops and emit findings / concerns / proposals rather than code.
- **Strict path and output contracts:** multiple personas are forbidden from writing outside canonical doc paths; JSON-only personas fail if they emit prose; tool-using personas fail if they only describe changes without landing files.
- **Architectural-threshold routing:** `architect` is invoked only when PM output meets explicit thresholds (`>=3` child stories, any `infra` scope, or titles mentioning schema/migration/dependency/rewrite/architecture`).
- **Model routing is persona-specific:** `factory/routes.yaml` maps persona names to model IDs under `azure_routes` and `routes`; Azure is the active default provider in provided source.
- **Failure behavior is often explicit in prompt contracts:** e.g. `dev` must emit `TESTS_NEED_CLARIFICATION:` when tests are wrong; `test_implementer` must distinguish real red tests from harness breakage; `release_manager` must refuse unsafe deploy plans.

## Key files

- `factory/routes.yaml` — runtime model routing and output-token caps for all personas.
- `factory/personas/pm.md` — direction triage and child-story decomposition contract.
- `factory/personas/sm.md` — story-file generation contract from PM output.
- `factory/personas/test_designer.md` — pre-implementation test-plan schema and anti-slop rules.
- `factory/personas/test_implementer.md` — writes tests to disk, runs suite, reports red/slop state.
- `factory/personas/dev.md` — writes production code only, cannot modify tests, must leave self-summary.
- `factory/personas/reviewer.md` — strong-model PR verdict with code/test-quality findings.
- `factory/personas/tech_writer.md` and `factory/personas/architect.md` — context rewrite personas for app docs/current-state.
- `factory/personas/bug_hunter.md`, `ralph.md`, `security.md`, `ux_auditor.md` — scheduled finding-generation personas.
- `factory/personas/factory_improver.md`, `manager_watcher.md`, `manager_summarizer.md`, `manager_diagnostician.md` — factory self-observation and escalation stack.

## Failure modes

- **JSON/protocol mismatch:** JSON-only personas (`pm`, `sm`, `analyst`, `architect`, `reviewer`, `release_manager`, etc.) emit prose, wrong schema, or forbidden fields. Symptom: chain/parser rejects output; downstream stage cannot start.
- **No-op tool persona runs:** `dev`, `test_implementer`, or `onboarder` talk about intended changes but do not write files to disk. Symptom: empty `git diff` / `git status`; run is marked failed or story blocked.
- **Forbidden path writes:** `architect`, `tech_writer`, `dev`, `sm`, `onboarder` violate canonical path rules (`context/decisions/*`, archive/history docs, non-canonical `context/*`). Symptom: context enforcer or chain rejects entire output.
- **Oversized or mis-sized decomposition from PM/SM:** PM emits child stories above hard file/iteration thresholds or SM emits oversized story JSON. Symptom: chain rejection, token overflow risk, or downstream dev exhaustion.
- **False-red test stage:** `test_implementer` accepts harness/import/bootstrap failure as “red tests” instead of story-specific red assertions. Symptom: dev receives unusable baseline; repeated failures with `ImportError`, `ModuleNotFoundError`, broken `conftest`, missing DB driver.
- **Dev/test contract violation:** `dev` edits frozen tests or does not stop on impossible tests. Symptom: chain aborts to `BLOCKED_TESTS_NEED_CLARIFICATION` (exact blocked state name confirmed in `factory_improver.md` examples).
- **Truncated structured output:** personas with large JSON payloads (`sm`, `architect`, manager personas) can exceed model output caps from `routes.yaml` (`azure/gpt-5.4` 16k, DeepSeek 8k, Claude 32k). Symptom: partial JSON, parse errors, or documented concern around `finish_reason=length`.
- **Misrouted model tier / provider limitation:** Azure default uses `azure/gpt-5.4` for many personas; `azure/gpt-5.3-codex` is explicitly unrouted because `text_run` uses Chat Completions, not the Responses API. Symptom: unsupported-operation failures if routed incorrectly; not confirmed that any persona currently does this.
- **Unsafe deploy-plan acceptance:** if `release_manager` failed to refuse missing health/smoke/rollback commands or destructive shell patterns, deployment orchestration would execute an unsafe plan. Prompt says it must refuse; enforcement outside prompt not confirmed in provided source.

## Escalation paths

When a persona in the main app-delivery chain fails, the prompt contracts indicate mostly **chain-level rejection or blocking**, not autonomous recovery by the persona itself. Confirmed paths from provided source:

- **`dev` test conflict:** if tests are wrong, `dev` must emit `TESTS_NEED_CLARIFICATION:` and stop; the chain routes back to `Test-Designer`. If `dev` touches tests, the chain aborts to `BLOCKED_TESTS_NEED_CLARIFICATION`.
- **`test_implementer` slop or non-meaningful red:** it sets `slop_detected: true`; the chain routes the plan back to `Test-Designer`.
- **`reviewer` rejection:** returns `verdict: request_changes`; chain blocks approval and, for low `test_quality_score`, labels PR `needs-test-quality-fix` and bounces to `Test-Designer`.
- **`architect` / `tech_writer` forbidden paths:** `factory/context/enforcer.py` rejects non-canonical or forbidden `context_updates[].path` values, causing the chain to reject the entire output.
- **`release_manager` invalid plan:** should return `deploy_plan: []` with refusal rationale; deploy should not proceed.
- **Scheduled personas (`bug_hunter`, `ralph`, `security`, `ux_auditor`, `factory_improver`)** do not directly modify code or open GitHub issues. They emit structured findings or proposals; the factory’s schedulers / handlers convert those into directions, pinned issue updates, or persisted JSON artifacts.
- **Factory self-observation stack:** `manager_watcher` (L1, cheap) summarizes and may set `escalate_to_l2`; `manager_summarizer` (L2) emits a concern and may set `escalate_to_l3`; `manager_diagnostician` (L3) emits a single proposal diff or `escalate_to_human=true`. Only L3 has halt authority, and may request `request_halt: true`.
- **Operator intervention:** explicitly confirmed for manager halt flow (`factory resume` clears halt). For other persona failures, human intervention is implied through reviewing blocked stories, rejected outputs, pinned improvement issues, or PR review; exact operator UI/mechanism is not confirmed in provided source.

### Persona inventory: consumes, produces, model tier, breakpoints

- **`pm`** — consumes `Direction` + canonical context; produces classification/decomposition JSON; model: `azure/gpt-5.4` (direct fallback `deepseek/deepseek-chat`); breaks on poor story sizing, missing backpressure handling, malformed JSON.
- **`analyst`** — consumes `Direction` + PM JSON + context; produces epic/phases/metrics/risks JSON; model: `azure/gpt-5.4` (direct fallback `deepseek/deepseek-chat`); breaks by inventing requirements, replacing PM stories silently, non-observable metrics.
- **`architect`** — consumes PM result + direction + full context prelude; produces `context_updates` rewrites; model: `azure/gpt-5.4` (direct fallback `claude-opus-4-7`); breaks on forbidden paths, append/history framing, non-current-state diagrams.
- **`sm`** — consumes direction + PM JSON + scope-matched context; produces story-file JSON; model: `azure/gpt-5.4` (direct fallback `deepseek/deepseek-chat`); breaks on output-size overflow, invented ACs, wrong story target paths.
- **`ux_designer`** — consumes story + `flow.md` + full context; produces `flow_additions`/`ui_notes`; model: `azure/gpt-5.4` (direct fallback `claude-opus-4-7`); breaks by inventing UX gaps or proposing backend work.
- **`test_designer`** — consumes story + flow/api spec + context + gates; produces `test_plan` JSON; model: `azure/gpt-5.4` (direct fallback `claude-opus-4-7`); breaks by planning slop tests or omitting required E2E/API tests.
- **`test_implementer`** — consumes test plan + story + gates; writes test files and returns run report JSON; model: `azure/deepseek-v4-pro` (direct fallback `deepseek/deepseek-coder`); breaks on green pre-impl tests, harness-failure-as-red, or modifying production code.
- **`dev`** — consumes story path + repo path + context; writes production code, runs tests, emits `SELF_SUMMARY`; model: `azure/deepseek-v4-pro` for both standard/hard (direct fallback standard `deepseek-coder`, hard `claude-sonnet-4-6`); breaks on touching tests/docs, failing to land code, or not stopping on test contradictions.
- **`reviewer`** — consumes PR diff + story + test artifacts + context; produces verdict JSON; model: `azure/gpt-5.4` (direct fallback `claude-opus-4-7`); breaks if it misses slop tests, lacks file:line citations, or approves below threshold.
- **`tech_writer`** — consumes final PR diff + full context + story; produces context rewrite JSON; model: `azure/gpt-5.4` (direct fallback `deepseek/deepseek-chat`); breaks on non-canonical paths, preserving history, or stale diagrams.
- **`onboarder`** — consumes existing repo only; writes full canonical `context/` set; model: `azure/gpt-5.4` (direct fallback `claude-opus-4-7`); breaks on exceeding read/tool budgets or failing to land files.
- **`release_manager`** — consumes merged PR metadata + `DeployConfig`; produces deploy plan JSON; model: `azure/gpt-5.4` (direct fallback `deepseek/deepseek-chat`); breaks by accepting missing mandatory commands or destructive shell strings.
- **`bug_hunter`** — consumes app/app_config/factory root; runs configured scanners; produces findings JSON; model: `azure/gpt-5.4` (direct fallback `deepseek/deepseek-chat`); breaks when tools are missing, findings lack file:line provenance, or duplicate spam directions.
- **`ralph`** — consumes config + PRD/context/module docs + source + tests; produces drift JSON; model: `azure/gpt-5.4` (direct fallback `deepseek/deepseek-chat`); breaks by inventing drift without cited path/test.
- **`security`** — consumes app config + docs + source (+ direction on tagged runs); produces threat-model JSON; model: `azure/gpt-5.4` (direct fallback `claude-opus-4-7`); breaks on uncited findings or shallow static-analysis-style output.
- **`ux_auditor`** — consumes app config + extracted `flow.md` files + context; produces UX findings JSON; model: `azure/gpt-5.4` (direct fallback `claude-opus-4-7`); breaks because v1 wiring is text-only, so live-browser evidence may be unavailable.
- **`factory_improver`** — consumes recent redesign events, blocked stories, persona index, state-machine summary; produces improvement proposals with unified diffs; model: `azure/gpt-5.4` (direct fallback `deepseek/deepseek-chat`); breaks on free-text instead of diff or invented patch context.
- **`manager_watcher` / `manager_summarizer` / `manager_diagnostician`** — consume increasingly richer factory signals and source bundles; produce watcher notes, concern docs, and patch proposals respectively; models: all `azure/gpt-5.4` under Azure default, direct fallbacks `deepseek-chat`, `claude-sonnet-4-6`, `claude-opus-4-7`; break on over-escalation, invented evidence, invalid unified diffs, or speculative low-confidence fixes.
- **`factory_self_context`** — consumes module name/topic/source bundle; produces concise Markdown context module; model tier not explicitly routed in `factory/routes.yaml` (falls back to `defaults.azure_fallback` / `defaults.fallback` at runtime).