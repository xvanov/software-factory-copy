"""Auto-merge gate handlers — one module per gate.

A gate is a programmatic check the auto-merge worker runs against a PR
before adding (or removing) the gate's label. Every gate produces a
``GateResult`` record; the worker aggregates them via
``factory.chain.gates.evaluator.evaluate_all_gates``. The canonical label
list lives in ``evaluator.ALL_GATE_LABELS``; the merge-REQUIRED subset for
a given app comes from ``evaluator.required_gate_labels(app_config)``.

Every gate is dry-run aware: when the worker is dry, gates read flags
the chain handlers wrote into StoryRecord (e.g. ``test_run_passed``)
instead of spawning subprocesses. When the worker is real, gates may
shell out to the commands declared in ``apps/<app>/config.yaml.gates``.

The gates (one per file in this package):

  * tests_red_first_confirmed
  * tests_green
  * tests_meaningful
  * flow_verified
  * coverage_verified
  * lint_clean
  * format_clean
  * types_clean
  * docs_current
  * canonical_paths_only
  * smoke_green
"""

from __future__ import annotations

from factory.chain.gates.evaluator import (
    GateResult,
    PRContext,
    evaluate_all_gates,
    gate_label_for,
)

__all__ = ["GateResult", "PRContext", "evaluate_all_gates", "gate_label_for"]
