# Artifacts for D010

This direction expects the following fixture videos to be present before the factory chain runs. The factory's chain copies them to `backend/tests/fixtures/pushup_counter/` as part of the work.

All three files are checked in alongside this README:

| File | What it contains | Manually verified count |
| --- | --- | --- |
| `pushups_20.mp4` | User performing 20 pushups, end-to-end. | 20 reps |
| `pushups_25.mp4` | User performing 25 pushups, end-to-end. | 25 reps |
| `pushups_0.mp4` | Recording with no completed pushups (negative-case fixture: scene is present but no rep cycle occurs). | 0 reps |

## Requirements

- Format: H.264 in `.mp4`, ≤ 60 fps.
- Duration: 30–90 seconds per rep video; 10–15 seconds for blank.
- File size: ≤ 30 MB each.
- Resolution: at least 480p; 720p preferred.
- Lighting: consistent — no extreme dark / overexposed regions.

## Why these specific fixtures

The acceptance criteria in `direction.md` assert specific `verify()` outcomes for `(criteria, upload)` pairs drawn from these three videos. The fixtures are the ground truth — the generated CV algorithm doesn't have to be perfect at counting in the wild, only correct against these manually-verified clips. If you want stronger guarantees later, add more fixtures and extend the assertion table in `direction.md`.
