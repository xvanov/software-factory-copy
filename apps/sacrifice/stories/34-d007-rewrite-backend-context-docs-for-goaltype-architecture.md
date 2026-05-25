# Story
- Document the canonical context updates for the pluggable GoalType interface and registry endpoint.

# Canonical Paths
- `context/current-state.md`
- `context/modules/backend.md`

# Acceptance Criteria
- The Sacrifice backend currently branches on `goal.goal_type` in `backend/app/routes/goals.py` and hard-codes one worker module per type in `backend/app/workers/`.
- Adding a new goal type today means editing the route's `if/elif` branches, adding a worker module, adding a schema in `backend/app/schemas/proof.py`, and wiring a frontend submission screen.
- That is a hard barrier to the dynamic goal-type generation flow in D010: a coding agent cannot extend a route's `if/elif` cleanly across multiple files.
- We need a plugin contract so a new goal type lives in a single directory and the route discovers it through a registry.
