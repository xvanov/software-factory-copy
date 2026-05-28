# Observability — signals, schemas, sinks, and consumers

## Overview

The factory’s observability layer is split between global append-only NDJSON streams under `state/events/` and per-story JSONL audit logs under `state/logs/`. The NDJSON streams are the manager-facing telemetry surface for fleet-level monitoring and detector tools; the per-story logs are the chain-facing forensic trail used to explain how one story progressed or failed.

All writers are explicitly best-effort: signal or log write failures must not break orchestrator/handler execution. In the provided source, downstream consumers are detector functions in `factory.manager.detectors.*` and the `factory why <id>` workflow described in `factory/chain/event_log.py`.

## Key concepts

- **Canonical signal directory:** `state/events/` holds six append-only NDJSON streams: `runs.ndjson`, `ticks.ndjson`, `queue.ndjson`, `webhooks.ndjson`, `git.ndjson`, and `spend.ndjson`.
- **Common event envelope:** every stream record written via `factory/manager/signals.py` includes at least `ts` (ISO-8601 UTC string), `schema_version` (`1`), and `event` (discriminator string).
- **Best-effort writes:** `write_event()` and `log_story_event()` swallow I/O failures and emit diagnostics to `stderr`; observability must not crash a real tick or handler.
- **Global vs per-story telemetry:** `state/events/*.ndjson` is cross-cutting operational telemetry; `state/logs/<story_id>-<slug>.log` is a story-local audit trail used for diagnosis/explanation.
- **Detector model:** manager detectors are pure readers over Phase 1 streams. They return structured observations only; decision-making is left to L1/L2/L3 agents.
- **String-based time filtering:** several detectors compare ISO timestamps lexicographically (`ts < since.isoformat()` / `>=`), so timestamp formatting consistency matters.
- **Fallback semantics:** `cost_spike` prefers `spend.ndjson` but falls back to summing `cost_usd` from `runs.ndjson` if the spend stream is absent or empty.
- **Partial wiring:** `signals.py` documents stream wiring sites; not all producer implementations are included here, so some payload details are only confirmed at wrapper level.

## Key files

- `factory/manager/signals.py` — central NDJSON writer and per-stream convenience wrappers for global event streams.
- `factory/chain/event_log.py` — per-story append-only JSONL audit log writer/reader under `state/logs/`.
- `factory/manager/detectors/__init__.py` — detector registry (`DETECTORS`) and doc extraction (`DETECTOR_DOCS`) for manager agents.
- `factory/manager/detectors/runs_failed_since.py` — returns raw failed `runs.ndjson` rows since a timestamp.
- `factory/manager/detectors/retry_storm.py` — groups failed run events by `(story_id, persona)` and surfaces counts/error excerpts.
- `factory/manager/detectors/cost_spike.py` — computes recent vs baseline spend from `spend.ndjson` or fallback `runs.ndjson`.
- `factory/manager/detectors/state_distribution_skew.py` — reads latest `queue_snapshot` per app from `queue.ndjson`.
- `factory/manager/detectors/tick_duration_outliers.py` — pairs `tick_start`/`tick_end` in `ticks.ndjson` and surfaces outliers/stuck ticks.
- `factory/manager/detectors/worktree_orphans.py` — combines `state/worktrees/` filesystem scan with `state/factory.db` story state lookup.

## Failure modes

- **Signal write silently lost:** if `state/events/` cannot be created or appended to, `write_event()` prints `[signals] ... failed` to `stderr` and drops the record. Symptom: missing telemetry with no state-machine failure.
- **Non-JSON-serializable payload fields:** `write_event()` falls back to `repr()` per offending value and logs a stderr warning. Symptom: record exists but some fields are stringified, which may degrade downstream parsing/aggregation.
- **Per-story log unavailable:** `log_story_event()` swallows `OSError` and prints `[event_log] failed to write ...`. Symptom: `factory why <id>` has incomplete or empty evidence even though story processing continued.
- **Malformed NDJSON/JSONL lines:** readers skip malformed NDJSON in detectors; `read_story_events()` inserts `{"event": "malformed_log_line", "raw": ...}` for bad story-log lines. Symptom: missing observations or degraded forensic quality.
- **Timestamp format drift:** detectors rely on lexicographic comparison of ISO strings in several places (`runs_failed_since`, `retry_storm`, `state_distribution_skew`, `cost_spike`). If producers emit non-comparable formats, window filtering becomes incorrect.
- **Missing or sparse baseline data:** `cost_spike()` returns neutral-ish values (`ratio = 1.0` when no data, `inf` when recent spend exists and baseline is zero). Symptom: detector output can be hard to interpret without agent context.
- **Tick pairing ambiguity:** `tick_duration_outliers()` indexes starts/ends by `tick_id`; duplicate IDs or missing `tick_end` records produce overwritten matches or “still_running” false positives. Duplicate-ID behavior is not guarded in provided source.
- **DB unavailable during orphan scan:** `worktree_orphans()` treats SQLite errors as `db_state = "missing"`. Symptom: many worktrees may appear orphan-like even when DB access, not actual orphaning, is the problem.

## Escalation paths

When observability components fail, the implementation favors **containment over escalation**: writers do not raise, and the orchestrator/handlers should continue executing. The immediate notification path confirmed in source is `stderr` output from `factory/manager/signals.py` and `factory/chain/event_log.py`.

For global telemetry loss:
- No automatic state transition is confirmed in the provided source.
- Manager-side detectors will see partial or empty data and may under-report anomalies.
- Operator intervention is manual: inspect `stderr`, verify permissions/path health for `state/events/`, and backfill or regenerate context only where possible. Historical gaps in append-only streams are not automatically repaired.

For per-story audit-log loss:
- Story execution continues, but the operator loses explanatory breadcrumbs for `factory why <id>`.
- No explicit notification beyond `stderr` is confirmed.
- Intervention is manual: inspect `state/logs/` path health and use DB state plus any surviving global streams to reconstruct story history.

For detector anomalies:
- Detectors themselves do not escalate; they “never make decisions” and only return observations.
- The next escalation hop is the manager’s L1/L2/L3 agents, which consume `DETECTORS` / `DETECTOR_DOCS` and decide whether an observation is anomalous enough to act on. Exact notification channels or state transitions are not confirmed in provided source.

For stream-specific issues, the expected operator workflow is:
- **`runs.ndjson` problems:** check persona-call accounting and runner integration (`factory/runner.py — _record_run()`, per wiring summary).
- **`ticks.ndjson` / `queue.ndjson` / `spend.ndjson` problems:** inspect orchestrator tick emission (`factory/chain/orchestrator.py — tick()` per wiring summary).
- **`webhooks.ndjson` problems:** inspect `factory/webhook/github.py`; current source says placeholder emitted.
- **`git.ndjson` problems:** inspect worktree lifecycle and commit/push sites (`factory/chain/worktree.py` and `handlers.py` per wiring summary).

No stronger automatic failover, paging path, or state-machine rollback on observability failure is confirmed in the provided source.