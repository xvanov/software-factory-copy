## Edit contract for navigation.md

This is a content-edit direction modeled as a `PUT` on a file resource — the factory's backpressure validator requires HTTP shape, so the rewrite is expressed in that vocabulary. The real spec is the structural contract that follows.

### Operation

- **Method**: PUT
- **Path**: /context/navigation.md (relative to repo root in `~/sacrifice/`)
- **Request body**: the full rewritten markdown content (replace, not append)
- **Response**: 200 OK on successful rewrite, with the new file content as the response body
- **Side effects**: none outside `context/navigation.md`; canonical-paths enforcer must pass

### Structural contract for the rewritten content

For each existing `## When working on <X>` section in the file, the new content must contain TWO subsections in this exact order:

#### Context files
- bullet list of `context/*.md` paths (existing entries preserved if still accurate)

#### Code files
- bullet list of repo-relative code paths relevant to this task scope

### Example shape

```
## When working on goals and verification

### Context files
- `context/modules/backend-app.md`
- `context/modules/backend-workers.md`
- `context/current-state.md`

### Code files
- `backend/app/routes/goals.py`
- `backend/app/models/goal.py`
- `backend/app/schemas/goal.py`
- `backend/app/workers/verification.py`
- `frontend/services/api.ts`
- `frontend/App.tsx`
```

### Acceptance signals

- `200` response (file successfully rewritten)
- No `4xx` response (no validator/enforcer rejections)
- No `5xx` response (no chain crash)
