# Post-mortem — sacrifice TDD chain, 2026-05-29/30

## Goal & outcome
**Goal:** ship the sacrifice backlog (deadline: 4am EDT / 08:00 UTC May 30).
**Outcome: MISSED.** Deadline passed ~12h before this stop. **Zero net new
stories deployed this session** — `deployed` stayed at 8 throughout (and 7 of
those 8 are *docs*-chain stories; only 1 TDD/code story has ever deployed,
story 4). 2 stories blocked; ~32 TDD stories never completed.
**Spend:** ~$72 (of $200 daily cap). **Elapsed:** ~16h. Chain now stopped.

## The deepest finding
The factory's **TDD code pipeline has effectively never worked end-to-end**
(1/33 deployed). The deploy count was carried almost entirely by the **docs
chain** (single-shot `docs_onboarder`, which bypasses the test→dev→review
loop). The 1 TDD success (story 4) was a self-contained backend unit with no
dependencies and a self-consistent contract. Everything else stalled.

## Root-cause chain (each wall found → fixed; 12 PRs merged #7–#18)
1. **FMS halt (loop-2, correct):** transient `TemplateNotFound` from a
   concurrent `uv` relink; L3 halted + escalated. (env self-healed)
2. **Dev infra storm** — `handle_dev` read `test_run_passed` but not
   `success`, so pre-model sandbox failures burned the retry budget. → circuit
   breaker (#7).
3. **FMS placeholder self-amplification** — manager prompts echoed the
   detector's own markers → 10× wasted opus escalations (#8).
4. **No-change / contradictory tests** — dev made 0 changes on unsatisfiable
   tests (404-vs-501, active-vs-draft) and churned to a block → route to
   test-repair (#9).
5. **Review non-convergence** — raw cycle-count cap blocked mid-progress →
   stability-based guard + code/test finding routing (#10/#13).
6. **Throughput** — serial single loop; safe **sharded** parallel loops +
   per-handler **hang timeout** + hourly cap (#10/#11).
7. **Azure `deepseek-v4-pro` rate-limit** — over-provisioning to 8–12 agents
   blew the 500K TPM; reduced to ≤3 concurrent (operational, not the model).
8. **Test harness** — unwritable `/var/sacrifice` `media_dir`, stale-`.pyc`
   contamination, wrong-contract assertions → isolated test env + contract
   grounding (#12).
9. **Reviewer never approved** — tagged style nitpicks medium/high → severity
   rubric (block on substance, not style) (#13).
10. **Chain Phases 1–4:** E2E gated on a real harness (`e2e_harness_ready`);
    contract-grounding + scope-fence moved up into `test_designer`;
    programmatic plan validator; slop→re-plan routing (#14–16).
11. **Reviewer harness/scope-awareness** — resolved the playwright-vs-pytest
    contradiction + sibling-story finding leakage (#17).
12. **Dependency ordering** — build foundations before dependents within a
    direction (id-order = SM build order) (#18).

Every fix was real and individually validated (e.g. story 18 reached
`tests_green` with a clean, harness-aware plan and a contradiction-free review).

## What's still UNRESOLVED (why it's not "clean")
Despite all 12 PRs, **no story converges end-to-end.** Stories cycle
`test_impl → tests_red → dev → test_design → …` without reaching
green-and-approved-and-merged. Concretely at stop:
- Foundations (story 14 model, 22 chat-base) **cycling in `test_impl`** — they
  hit slop, re-plan, occasionally reach `tests_red`, dev runs, then bounces
  back to test-repair. They do not deploy, so dependents stay (correctly)
  deferred.
- 2 stories blocked on **slop** (test_implementer writes tests that pass
  before implementation — not red-first) even after the re-plan path.

## Leading hypotheses for the non-convergence
- **(a) Model capability on the last mile** — deepseek hits 95–98% of tests
  but can't close the final assertion / can't reliably write red-first tests
  for these stories; the dev↔test loop never settles.
- **(b) Spec quality** — several stories carry contradictory or aspirational
  contracts (the api_spec examples that misled tests); they may need human
  rework, not more chain iterations.
- **(c) Missing capabilities** — no Playwright/frontend test harness, so
  frontend/E2E stories have no runnable oracle at all (deferred to backend
  slices, which leaves pure-UI behavior untestable here).

## Recommendations
1. **Harden ONE foundation by hand** (story 14, D008 model — simplest, no
   deps) all the way to `deployed`, to identify the *true* final blocker with
   certainty before more automation.
2. **Decide the model question with data:** if dev can't converge clean
   stories, trial a stronger dev model on a few (cost vs. throughput).
3. **Spec-quality pass:** human review of D008–D010 `api_spec.md` for the
   contradictions the chain keeps hitting (draft/active, 404/501, cross-
   direction path flips).
4. **Build a real Playwright harness** (or formally scope frontend/E2E out of
   the pytest chain) so those stories have an oracle.
5. **Explicit dependency metadata** on stories (vs the id-order heuristic) +
   cross-direction ordering.

## Durable value delivered
The 12 PRs are real, tested factory-robustness improvements that benefit every
future run regardless of this batch: infra resilience (circuit breaker, hang
timeout, auto-recovery), FMS calibration, safe parallelism, test isolation,
contract-grounded test design, calibrated review, and dependency ordering.

## Operational notes
- `pkill -f "<pattern>"` self-matches the invoking shell (exit 144) — kill
  factory daemons by explicit PID via `ps -eo pid,args | awk '$2=="bash" &&
  $3=="./scripts/drive_chain.sh"{print $1}'`.
- Multiple drive loops are only safe with `FACTORY_SHARD=k/n` (disjoint
  id-mod-n story sets); unsharded duplicates collide on the same worktree.
- Azure `deepseek-v4-pro` (dev/test_impl) rate-limits above ~3 concurrent.
