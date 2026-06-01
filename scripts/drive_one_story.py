"""Drive a SINGLE story through dev<->review and count review cycles.

Isolation harness for the Loop-4 convergence experiment: runs only the given
story id through handle_dev / handle_review (real LLM, azure provider), printing
each transition, and stops at REVIEWER_DONE (converged), a BLOCKED state, or a
safety cap. Reports the number of review rounds it took.

Usage: FACTORY_PROVIDER=azure uv run python scripts/drive_one_story.py <story_id>
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlmodel import Session, create_engine

from factory.app_config import load_app_config
from factory.chain import orchestrator as O
from factory.chain.state_machine import StoryRecord, StoryState

STORY_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 14
SAFETY_STEPS = 14  # plenty for dev->review a few rounds; convergence guard caps at 6 cycles

root = Path(".").resolve()
db = root / "state" / "factory.db"
cfg = load_app_config("sacrifice", root)

TERMINAL = {
    StoryState.REVIEWER_DONE.value,
    StoryState.TECH_WRITER_DONE.value,
    StoryState.PR_OPEN.value,
    StoryState.DEPLOYED.value,
    StoryState.BLOCKED_TESTS_NEED_CLARIFICATION.value,
    StoryState.BLOCKED_REVIEW_NONCONVERGENT.value,
}


def load() -> StoryRecord:
    eng = create_engine(f"sqlite:///{db}", echo=False)
    with Session(eng) as s:
        row = s.get(StoryRecord, STORY_ID)
        assert row is not None
        return row


print(f"=== driving story {STORY_ID} (only dev/review) ===", flush=True)
for step in range(SAFETY_STEPS):
    s = load()
    if s.state in TERMINAL:
        break
    name = O._dispatch_for_story(s)
    if name not in ("dev", "review"):
        print(f"[stop] state={s.state} dispatches '{name}' (past the dev/review loop)", flush=True)
        break
    before = s.state
    res = O._invoke_handler(name, s, cfg, root, dry_run=False, db_path=db)
    after = load()
    print(
        f"[step {step}] {name}: {before} -> {res.next_state.value} "
        f"| reviewer_cycles={after.reviewer_cycles} dev_retries={after.dev_retries}",
        flush=True,
    )

final = load()
review_rounds = final.reviewer_cycles + (
    1 if final.state in (StoryState.REVIEWER_DONE.value, StoryState.PR_OPEN.value) else 0
)
print(
    f"=== DONE: state={final.state} reviewer_cycles={final.reviewer_cycles} "
    f"(~{review_rounds} review rounds) dev_retries={final.dev_retries} ===",
    flush=True,
)
