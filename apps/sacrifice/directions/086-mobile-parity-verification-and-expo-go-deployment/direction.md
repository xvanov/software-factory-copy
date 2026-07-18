---
title: Mobile parity, native verification harness, and Expo Go deployment to iPhone
type: feature
priority: p1
explore: true
created_at: '2026-07-18T00:00:00+00:00'
---

# Mobile parity, native verification harness, and Expo Go deployment to iPhone

## Why

The frontend is an Expo/React Native codebase, but all recent product work
(chat-first goal creation, proof capture/upload affordances, goal-type
registry metadata, accessibility fixes) has only ever been exercised through
the WEB target (`expo start --web` / react-native-web). The native
iOS/Android target is unverified and almost certainly divergent: web-only DOM
API usage, missing `Platform.OS` branches, camera/upload paths that were only
tested against browser APIs, and an API base URL that assumes localhost. The
operator wants the app running, heavily verified, on a real iPhone via Expo
Go, with the backend reachable from the phone.

## Environment constraints (authoritative — do not fight these)

- The host is LINUX. There is NO Xcode and NO iOS simulator, ever. Do not
  create stories that require building iOS binaries or running an iOS
  simulator. iPhone delivery is via **Expo Go** (`expo start --tunnel`, QR
  scan) — no EAS builds, no Apple Developer account.
- The closest-to-real mobile runtime available locally is the **Android
  emulator** (provisioned by the operator on the host, with **Maestro** for
  native E2E). Native verification targets Android emulator; iOS-specific
  truth comes from the operator's physical iPhone via Expo Go.
- The backend stays on this host and is exposed to devices through a
  **Cloudflare tunnel** (operator-provisioned `cloudflared` systemd service).
  App code must read its API base URL from configuration
  (`EXPO_PUBLIC_API_URL`), never hardcode localhost.
- Do not break the web target: every native fix must keep web E2E green.
  Platform divergence belongs behind `Platform.OS` checks or `.native.tsx` /
  `.web.tsx` file splits, not forks of shared logic.

## Acceptance Criteria

- [ ] 1. **Parity audit is executed and codified**: an automated check (script
  + test) inventories web-only API usage in shared code paths (`document.`,
  `window.` outside platform guards, `localStorage`, DOM event types) and the
  core flows (register/login, goal creation including registry metadata,
  proof capture/upload, dashboard, chat-first creation) each have a
  native-compatible implementation. The check runs in CI/test gate and fails
  on new unguarded web-only API usage.
- [ ] 2. **API base URL is configuration**: the app resolves its backend from
  `EXPO_PUBLIC_API_URL` (with a sane localhost default for web dev). All
  fetch/auth/upload call sites go through one client module that honors it.
  Token storage uses `expo-secure-store` on native and falls back cleanly on
  web.
- [ ] 3. **Native E2E harness exists and is green**: `make mobile-e2e` (repo
  root) boots the backend (isolated port, same pattern as `make smoke`),
  launches the app on the Android emulator, and drives the core journey
  (register → login → create goal → activate → submit proof) via Maestro
  flows checked into `e2e/mobile/`. Exits non-zero on any step failure.
- [ ] 4. **Camera/media proof path works natively**: proof capture uses
  `expo-camera`/media-library on native (not browser file inputs), uploads
  succeed against the backend, and the Maestro flow covers a
  submit-proof-with-media step (emulator virtual camera acceptable).
- [ ] 5. **Expo Go serving is a service**: `make mobile-serve` starts
  `expo start --tunnel` non-interactively, writes the connection URL + QR
  payload to `logs/expo-go-connection.txt`, and stays healthy in the
  background (documented systemd user unit or equivalent). `make
  mobile-serve-status` reports whether the tunnel and Metro bundler are up.
- [ ] 6. **On-device diagnostics screen**: a lightweight in-app screen (dev
  builds only) shows the resolved API URL, backend `/api/health` status,
  platform/OS, and app version — so a human holding the phone can verify
  connectivity in one glance without a debugger.
- [ ] 7. **iPhone runbook**: `context/mobile-runbook.md` documents the full
  operator path — start services, scan QR with iPhone, install Expo Go,
  verify via the diagnostics screen, run through the core journey — plus
  troubleshooting (tunnel down, Metro cache, LAN vs tunnel mode).
- [ ] 8. **Full-journey native verification against the tunnel**: the Maestro
  core-journey flow passes on the Android emulator with the app pointed at
  the PUBLIC tunnel URL (not localhost), proving the same path an iPhone will
  take end-to-end.

## Sequencing guidance for PM

Decompose roughly as: (1) API-client/config unification [AC2] and (2) parity
audit tooling [AC1] first — they unblock everything; then (3) native proof
capture [AC4]; then (4) Maestro harness [AC3]; then (5) Expo Go service +
diagnostics + runbook [AC5, AC6, AC7]; finally (6) the tunnel-pointed
full-journey verification story [AC8]. Keep stories single-scope and small;
the Maestro harness story should land the `make mobile-e2e` plumbing with ONE
flow, later stories extend flows rather than rebuild plumbing.

## Notes for downstream

- Host prerequisites (Android SDK + emulator AVD, Maestro CLI, cloudflared
  tunnel service, `EXPO_PUBLIC_API_URL` value) are provisioned by the
  OPERATOR outside story scope. Stories may assume `emulator`, `adb`,
  `maestro`, and `cloudflared` exist on PATH; if a gate command fails because
  a host tool is missing, that is an infra blocker to surface, not code to
  work around.
- The repo already has `e2e/` (web Playwright) — mobile flows live beside it
  in `e2e/mobile/`, not in a new top-level location.
- Reuse `scripts/smoke_journey.py` semantics for the backend-boot portion of
  `make mobile-e2e`; do not invent a second backend-boot mechanism.
