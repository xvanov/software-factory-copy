# manager — FMS escalation/apply loop

## Overview

The Factory Management System (FMS) is the factory’s self-observation and self-modification loop: L1 `watcher` summarizes recent signals, L2 `summarizer` turns escalations into structured concern documents, L3 `diagnostician` generates concrete proposals/patches, and L4 `apply` classifies and applies only bounded-safe changes. Safety is enforced by deterministic gates around live repo mutation, plus two stop mechanisms: `halt.py` (L3 may request; only a human may clear) and `circuit_breaker.py` (auto-revert + 24h halt of safe auto-apply after manager-caused regressions). The manager is explicitly allowed to reason about the factory, but not to directly edit its own control code.

## Key concepts

- **Four-level escalation pipeline**
  - L1 `run_watcher_once`: minute-scale monitoring over recent event streams and detector outputs.
  - L2 `run_summarizer_once`: consumes `watcher_notes` with `escalate_to_l2=true` and emits structured concerns.
  - L3 `run_diagnostician_once`: consumes escalated concerns and produces a proposal with `suggested_patch`.
  - L4 `apply_manager_proposals`: deterministic classification and application of proposal files under `state/manager_proposals/*.json`.

- **LLM judgment lives in personas, not plumbing**
  - `watcher.py`, `summarizer.py`, and `diagnostician.py` all describe themselves as plumbing only.
  - Anomaly judgment is delegated to persona prompts such as `factory/personas/manager_watcher.md`, `manager_summarizer.md`, and `manager_diagnostician.md`.
  - The Python layer assembles context bundles, calls `factory.runner.text_run`, and persists outputs.

- **Context assembly is evidence-heavy and continuity-aware**
  - L1 reads raw streams `runs`, `ticks`, `queue`, `webhooks`, `git`, `spend`, caps lines per stream (`_MAX_LINES_PER_STREAM = 200`), truncates long payload strings (`_PAYLOAD_STRING_CAP = 500`), and includes recent `watcher_notes` for continuity.
  - L1 also includes detector outputs plus inline detector docstrings from `factory.manager.detectors.DETECTOR_DOCS`.
  - L2 reads flagged watcher notes, the underlying signals in the flagged windows, prior concern documents, and detector docstrings again so the L2 model can interpret evidence shapes.
  - L3 pre-loads source files based on the concern’s `proposed_area` via `_pre_load_source`.

- **Persistent artifacts and event streams**
  - L1 writes notes to `state/events/watcher_notes.ndjson`.
  - L2 writes concern events to `state/events/concerns.ndjson` and concern documents under `state/concerns/*.json`.
  - L3 writes proposals under `state/manager_proposals/*.json` and proposal events to `state/events/proposals.ndjson`.
  - L4 records apply history in `state/.manager_apply_history.json`; circuit breaker records tracked commits in `state/.manager_commits.ndjson`.

- **L3 proposal schema includes halt authority**
  - Required fields include `concern_title`, `diagnosis`, `proposal`, `target_class`, `escalate_to_human`, and `escalation_reason`.
  - `proposal.kind` is constrained to `prompt_edit`, `persona_settings`, `dispatch_code`, `detector_tool`, `observability`, or `doc_update`.
  - Optional Phase 7 fields: `request_halt` and `halt_reason`; only L3 may set these.

- **Deterministic L4 classifier is the main circuit-breaker before repo mutation**
  - `_classify_manager_proposal` is intentionally the only rule-based component of the FMS.
  - Classes:
    - `safe`: only `prompt_edit`, `persona_settings`, `detector_tool`, and only if class-specific validation passes.
    - `risky`: always `dispatch_code`, or any nominally safe class that fails validation.
    - `forbidden`: manager self-edit paths, apply-pipeline paths, or `escalate_to_human=true`.
    - `escalate_to_human`: explicit operator handoff when `target_class == "escalate_to_human"`.

- **Manager self-edit restrictions**
  - Patches touching `factory/manager/.+\.py`, `factory/chain/factory_improver_apply.py`, or `factory/manager/apply.py` are forbidden.
  - Flat `factory/manager/*.py` files are always forbidden.
  - There is a narrow carve-out for new detector files under `factory/manager/detectors/*.py` when the diff creates a new file; modifying existing detector files remains forbidden.

- **Two stop mechanisms**
  - `halt.py`: writes `state/factory_mode.json` with `mode: "halted"`, `set_by: "manager_diagnostician"`, `concern_title`, `proposal_path`, and `reason`. `is_halted()` is checked by the driver loop and `tick()` before dispatching.
  - `circuit_breaker.py`: if tests fail and `HEAD` matches a tracked manager-authored commit, auto-reverts on branch `factory-manager-revert/<ts>`, opens a PR, writes `state/circuit_breaker.json`, and blocks safe auto-apply for 24 hours.

## Key files

- `factory/manager/watcher.py` — L1 monitor that reads recent event streams and detector results, builds an LLM context bundle, and emits watcher notes with escalation decisions.
- `factory/manager/summarizer.py` — L2 concern builder that consumes escalated watcher notes, reconstructs evidence windows, and writes structured concern documents/events.
- `factory/manager/diagnostician.py` — L3 frontier-tier proposal generator that loads concern context plus source files selected by `proposed_area` and emits proposal JSON, including optional halt requests.
- `factory/manager/apply.py` — L4 deterministic proposal classifier/apply loop; enforces safe/risky/forbidden routing, history tracking, and recursion-safety boundaries.
- `factory/manager/halt.py` — halt-state authority; only writes/reads/clears `state/factory_mode.json` and archives halt history in `state/.halt_history.json`.
- `factory/manager/circuit_breaker.py` — post-apply regression guard that tracks manager-authored commits, trips on manager-caused test failures, opens revert PRs, and suppresses safe auto-apply while active.

## Failure modes

- **Watcher misses relevant evidence due to windowing or truncation**
  - Conditions: event files exceed `_MAX_LINES_PER_STREAM`, payloads exceed `_PAYLOAD_STRING_CAP`, timestamps are malformed/non-ISO, or stream files are unreadable.
  - Symptom: L1 summaries understate or miss anomalies; `escalate_to_l2` may remain false despite raw issues in logs.

- **Summarizer produces no concern despite flagged watcher notes**
  - Conditions: no new flagged notes after `_last_concern_ts`, malformed `watcher_notes.ndjson`, unreadable `state/concerns/`, or L2 model/schema failure.
  - Symptom: `run_summarizer_once` returns `None` or emits no concern documents; escalation chain stalls between L1 and L2.

- **Diagnostician context is degraded or mis-scoped**
  - Conditions: `proposed_area` is wrong/`unknown`, `_pre_load_source` omits relevant files, source bundle truncates at `_SOURCE_FILE_CAP`/`_BUNDLE_TOTAL_CAP`, or concern fields are malformed.
  - Symptom: L3 produces low-quality diagnosis/patches, wrong `target_class`, unnecessary human escalation, or inappropriate halt requests.

- **Safe proposal blocked or downgraded by L4 validation**
  - Conditions: diff is not valid unified diff, target paths fall outside allowed subsets, heading removal in `prompt_edit`, out-of-clamp numeric edits in `persona_settings`, or detector patch modifies existing forbidden manager files.
  - Symptom: proposal classified `risky` or `forbidden` rather than `safe`; no auto-apply occurs.

- **Manager self-edit recursion attempt**
  - Conditions: L3 proposes edits under `factory/manager/*.py`, nested `factory/manager/**.py`, `factory/manager/apply.py`, or `factory/chain/factory_improver_apply.py`.
  - Symptom: L4 classifies proposal as `forbidden`; self-modification of control-plane code is prevented.

- **Circuit breaker trips after manager-caused regression**
  - Conditions: tests fail under `check_and_trip`, and `git rev-parse HEAD` matches a SHA previously recorded by `record_manager_commit`.
  - Symptom: revert branch/PR created, `state/circuit_breaker.json` written with `halt_until`, and subsequent safe applies logged as `status=apply_pipeline_halted_by_circuit_breaker`.

- **Halt becomes active and dispatch stops**
  - Conditions: L3 proposal sets `request_halt=true` with a `halt_reason`, and downstream logic invokes `request_halt`.
  - Symptom: `state/factory_mode.json` exists with `mode == "halted"`; driver loop and `tick()` refuse to dispatch until a human clears it.

## Escalation paths

Normal escalation path:

1. **L1 Watcher**
   - Runs every minute or on demand (`factory manager watch`).
   - Reads recent streams plus detector outputs/docstrings.
   - Writes a watcher note to `state/events/watcher_notes.ndjson`.
   - If the note sets `escalate_to_l2=true`, it becomes input for L2.

2. **L2 Summarizer**
   - Runs every 3 minutes or immediately when L1 escalates.
   - Reads flagged watcher notes since the last concern, reconstructs underlying signal windows, and emits a concern document plus `concern_emitted`-style eventing in `state/events/concerns.ndjson` (exact event name not confirmed in provided source, but `_last_concern_ts` reads the stream for the latest concern timestamp).
   - If the concern sets `escalate_to_l3=true`, it becomes eligible for L3 processing.

3. **L3 Diagnostician**
   - Runs only when there are unprocessed escalated concerns.
   - Pre-loads code/docs based on `proposed_area`, calls a frontier model, and writes a proposal under `state/manager_proposals/`.
   - Proposal may:
     - target a safe/risky/forbidden class for L4 handling,
     - explicitly `escalate_to_human`,
     - or request a halt via `request_halt`/`halt_reason`.

4. **L4 Apply**
   - Deterministically classifies proposals.
   - `safe` proposals may be auto-applied, committed, and recorded for breaker tracking via `record_manager_commit`.
   - `risky` proposals require operator review/PR flow rather than auto-apply.
   - `forbidden` proposals are rejected.
   - `escalate_to_human` proposals are handed off directly.

Failure / safety escalation:

- **If L3 requests a halt**
  - `request_halt()` writes `state/factory_mode.json` with `mode: "halted"` and archives any prior halt state if already present.
  - The factory’s driver loop and `tick()` check `is_halted()` before dispatching; dispatch stops.
  - Only a human operator should clear it using `clear_halt()` (intended via `factory resume`), which archives the halt entry into `state/.halt_history.json`.

- **If a manager-authored commit causes regressions**
  - `check_and_trip()` runs tests; if failure is attributable to the current `HEAD` being a tracked manager commit, it reverts on a new branch, opens a PR, and writes `state/circuit_breaker.json`.
  - While `is_tripped()` is true, `apply_manager_proposals` refuses to auto-apply safe proposals. Risky/operator-reviewed proposals are not blocked by the breaker.

- **Operator intervention**
  - Review concern docs in `state/concerns/`, proposal JSON in `state/manager_proposals/`, halt state in `state/factory_mode.json`, breaker state in `state/circuit_breaker.json`, and apply history in `state/.manager_apply_history.json`.
  - Clear halt only through the operator path (`clear_halt`, intended via `factory resume`).
  - Merge or reject revert PRs opened by the circuit breaker.
  - Human escalation is also explicitly encoded in proposal fields (`target_class == "escalate_to_human"` or `escalate_to_human=true`).