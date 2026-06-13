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
- Status: Complete
- Notes: docker-compose.yml backend service bind-mounts ~/software-factory/apps/sacrifice/directions → /var/factory/directions (rw) with FACTORY_DIRECTIONS_PATH env var. Created app/services/directions.py with write_direction() that creates named direction directories with direction.json payload and .manifest file at the configured path. Config default is /var/factory/directions, overridable via FACTORY_DIRECTIONS_PATH env. Tests cover compose volume (exact source/target/rw), config defaults and overrides, and direction writing behavior (create, explicit base path, overwrite). No prod compose variant present.
- File List: docker-compose.yml, backend/app/config.py, backend/app/services/directions.py, backend/tests/test_factory_directions_volume.py 

## Senior Developer Review
- Status: Pending
- Notes: 

## Review Follow-ups
- None.
