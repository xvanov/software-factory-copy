# Story
## Title
D010 compose bind-mount for factory directions volume

## Acceptance Criteria
- `docker-compose.yml` (and prod variant if present) bind-mounts `~/software-factory/apps/sacrifice/directions/` into the Sacrifice backend container at the configured path (rw).
- Backend writes the synthesized direction directory to a configurable path (default mounted at `/var/factory/directions/` inside the Sacrifice container; bound to `~/software-factory/apps/sacrifice/directions/` on the host).

## Tasks / Subtasks
- [ ] Update `docker-compose.yml` backend service with rw bind mount.
- [ ] Update prod compose variant if present.
- [ ] Ensure mounted container path matches backend config default/override.
- [ ] Verify directory exists/boot behavior is documented in compose comments or env wiring.
- [ ] Add/adjust env configuration for directions path if required.
- [ ] Smoke-check container sees host-written files and vice versa.

## Dev Notes
### flow.md
[flow.md: see d010-request-new-goal-type-endpoint-creates-pending-goal Dev Notes for verbatim embed]

### api_spec.md
[api_spec.md: see d010-add-awaiting-goal-type-goal-status-and-direction-linkage Dev Notes for verbatim embed]

### Context pointers
- [Source: context/project.md#Top-level layout]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on overall repository shape]

### Direction acceptance criteria (verbatim)
- `docker-compose.yml` (and prod variant if present) bind-mounts `~/software-factory/apps/sacrifice/directions/` into the Sacrifice backend container at the configured path (rw).
- Backend writes the synthesized direction directory to a configurable path (default mounted at `/var/factory/directions/` inside the Sacrifice container; bound to `~/software-factory/apps/sacrifice/directions/` on the host).

## References
- `docker-compose.yml`
- `backend/app/config.py`

## Dev Agent Record
- Status: Not started
- Notes: 

## Senior Developer Review
- Status: Pending
- Notes: 

## Review Follow-ups
- None.
