"""factory.manager.detectors — seed detector tools (Phase 2).

Each detector is a pure function that reads the Phase 1 signal streams
and returns structured *observations*.  Detectors never make decisions;
they describe what the data says.  The L1/L2/L3 agents call these
functions and decide whether an observation is anomalous in context.

Registry
--------
``DETECTORS``
    Maps detector name → callable.  Agents iterate this to discover
    available tools.

``DETECTOR_DOCS``
    Maps detector name → docstring (via ``inspect.getdoc``).  Agents
    render these as tool descriptions so the LLM knows what each
    detector surfaces without reading the source.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable

from factory.manager.detectors.cost_spike import cost_spike
from factory.manager.detectors.placeholder_prompts import placeholder_prompts
from factory.manager.detectors.retry_storm import retry_storm
from factory.manager.detectors.review_churn import review_churn
from factory.manager.detectors.runs_failed_since import runs_failed_since
from factory.manager.detectors.stalled_stories import stalled_stories
from factory.manager.detectors.state_distribution_skew import state_distribution_skew
from factory.manager.detectors.tick_duration_outliers import tick_duration_outliers
from factory.manager.detectors.worktree_orphans import worktree_orphans

__all__ = [
    "DETECTORS",
    "DETECTOR_DOCS",
    "cost_spike",
    "placeholder_prompts",
    "retry_storm",
    "review_churn",
    "runs_failed_since",
    "stalled_stories",
    "state_distribution_skew",
    "tick_duration_outliers",
    "worktree_orphans",
]

DETECTORS: dict[str, Callable] = {
    "runs_failed_since": runs_failed_since,
    "retry_storm": retry_storm,
    "review_churn": review_churn,
    "cost_spike": cost_spike,
    "tick_duration_outliers": tick_duration_outliers,
    "state_distribution_skew": state_distribution_skew,
    "worktree_orphans": worktree_orphans,
    "placeholder_prompts": placeholder_prompts,
    "stalled_stories": stalled_stories,
}

DETECTOR_DOCS: dict[str, str] = {
    name: inspect.getdoc(fn) or ""
    for name, fn in DETECTORS.items()
}
