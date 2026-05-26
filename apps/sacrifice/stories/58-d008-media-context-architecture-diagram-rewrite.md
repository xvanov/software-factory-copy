# Story
- Write canonical documentation only under the `context/` tree for the camera capture pipeline direction.

# Canonical Paths
- `context/project.md`
- `context/current-state.md`
- `context/navigation.md`
- `context/modules/frontend.md`
- `context/modules/backend.md`
- `context/modules/mobile.md`

# Acceptance Criteria
- Sacrifice's first physical-world goal type (pushup-counter, the canonical novel case validated in D010) requires recording a video on the user's phone and uploading it to the backend for verification.
- The Expo frontend has no camera capability today and the backend has no media upload pipeline.
- Building this as shared infrastructure now means every future sensor-based goal type — timers, GPS tracks, photo proofs — reuses one tested capture component and one upload endpoint instead of inventing its own.
- Keeping it separate from D010 also prevents D010's "did the generator work" acceptance from being entangled with "did the camera plumbing work."
