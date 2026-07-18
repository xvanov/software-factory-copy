# Story
**Title:** Mobile parity, native verification harness, and Expo Go deployment to iPhone — broad read
**Slug:** mobile-parity-native-verification-harness-and-expo-go-deploy
**Scope:** frontend

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

### Testable Claims (EARS)
AC1.1: WHEN the parity audit check is executed, THE audit tooling SHALL inventory web-only API usage in shared code paths including `document.`, `window.` outside platform guards, `localStorage`, and DOM event types
AC1.2: WHEN register/login is exercised on native, THE frontend implementation SHALL be native-compatible
AC1.3: WHEN goal creation including registry metadata is exercised on native, THE frontend implementation SHALL be native-compatible
AC1.4: WHEN proof capture/upload is exercised on native, THE frontend implementation SHALL be native-compatible
AC1.5: WHEN dashboard is exercised on native, THE frontend implementation SHALL be native-compatible
AC1.6: WHEN chat-first creation is exercised on native, THE frontend implementation SHALL be native-compatible
AC1.7: WHEN new unguarded web-only API usage is introduced in shared code paths, THE CI/test gate SHALL fail
AC2.1: WHEN the app resolves its backend, THE app SHALL use `EXPO_PUBLIC_API_URL`
AC2.2: WHEN web development runs without explicit configuration, THE app SHALL provide a sane localhost default for web dev
AC2.3: WHEN fetch, auth, or upload call sites are used, THE frontend SHALL route them through one client module that honors the configured backend URL
AC2.4: WHEN token storage is used on native, THE app SHALL use `expo-secure-store`
AC2.5: WHEN token storage is used on web, THE app SHALL fall back cleanly on web
AC3.1: WHEN `make mobile-e2e` is executed from the repo root, THE harness SHALL boot the backend on an isolated port using the same pattern as `make smoke`
AC3.2: WHEN `make mobile-e2e` is executed, THE harness SHALL launch the app on the Android emulator
AC3.3: WHEN `make mobile-e2e` is executed, THE harness SHALL drive the core journey register → login → create goal → activate → submit proof via Maestro flows checked into `e2e/mobile/`
AC3.4: WHEN any mobile E2E step fails, THE command SHALL exit non-zero
AC4.1: WHEN proof capture is used on native, THE proof flow SHALL use `expo-camera`/media-library and not browser file inputs
AC4.2: WHEN native proof media is uploaded, THE upload SHALL succeed against the backend
AC4.3: WHEN the Maestro flow runs, THE flow SHALL cover a submit-proof-with-media step
AC5.1: WHEN `make mobile-serve` is executed, THE wrapper SHALL start `expo start --tunnel` non-interactively
AC5.2: WHEN `make mobile-serve` starts successfully, THE wrapper SHALL write the connection URL and QR payload to `logs/expo-go-connection.txt`
AC5.3: WHEN Expo Go serving is running, THE service SHALL stay healthy in the background
AC5.4: WHEN `make mobile-serve-status` is executed, THE wrapper SHALL report whether the tunnel and Metro bundler are up
AC6.1: WHEN a dev build displays the diagnostics screen, THE screen SHALL show the resolved API URL
AC6.2: WHEN a dev build displays the diagnostics screen, THE screen SHALL show backend `/api/health` status
AC6.3: WHEN a dev build displays the diagnostics screen, THE screen SHALL show platform/OS
AC6.4: WHEN a dev build displays the diagnostics screen, THE screen SHALL show app version
AC6.5: WHEN the app is not a dev build, THE diagnostics screen SHALL be unavailable
AC7.1: WHEN the operator follows `context/mobile-runbook.md`, THE runbook SHALL document the full operator path including start services, scan QR with iPhone, install Expo Go, verify via the diagnostics screen, and run through the core journey
AC7.2: WHEN the operator uses `context/mobile-runbook.md`, THE runbook SHALL include troubleshooting for tunnel down, Metro cache, and LAN vs tunnel mode
AC8.1: WHEN the Maestro core-journey flow runs on the Android emulator, GIVEN the app is pointed at the PUBLIC tunnel URL and not localhost, THE flow SHALL pass end-to-end
AC8.2: WHEN the Android emulator journey passes against the PUBLIC tunnel URL, THE verification SHALL prove the same path an iPhone will take end-to-end

## Tasks / Subtasks
- [ ] Establish frontend configuration contract for `EXPO_PUBLIC_API_URL`
  - [ ] Centralize backend URL resolution in one client/config module
  - [ ] Remove hardcoded localhost usage from app call sites
  - [ ] Preserve sane localhost default for web-only development
- [ ] Establish token storage parity
  - [ ] Add native storage adapter using `expo-secure-store`
  - [ ] Add clean web fallback adapter
  - [ ] Route auth persistence through the shared adapter
- [ ] Audit native compatibility in shared frontend flows
  - [ ] Inventory shared-path web-only API usage
  - [ ] Guard or split platform-specific code with `Platform.OS` or `.native`/`.web` files
  - [ ] Verify register/login flow remains native-compatible
  - [ ] Verify chat-first goal creation consumes backend metadata on native paths
  - [ ] Verify dashboard path remains native-compatible
- [ ] Land native proof capture UX
  - [ ] Use native camera/media-library path on native
  - [ ] Preserve browser file-input behavior on web
  - [ ] Expose upload progress and accepted-state handling in UI
  - [ ] Handle camera permission denial with library fallback messaging
- [ ] Add dev-only diagnostics surface
  - [ ] Show resolved API URL
  - [ ] Show `/api/health` status
  - [ ] Show platform/OS
  - [ ] Show app version
  - [ ] Exclude from non-dev builds
- [ ] Coordinate frontend support for mobile E2E and tunnel verification
  - [ ] Ensure app launch path works in Android emulator
  - [ ] Ensure app respects public tunnel backend URL
  - [ ] Keep web target green while adding native branches
- [ ] Confirm story-level verification hooks for downstream infra/test/docs slices
  - [ ] Frontend supports Maestro-driven register/login/create/activate/submit-proof journey
  - [ ] Frontend supports Expo Go operator diagnostics needs
  - [ ] Frontend surfaces clear connectivity failure states when backend is unreachable
  - [ ] Frontend routes expired/invalid token users back to login

## Dev Notes
### Scope framing
This broad-read story is the umbrella frontend source of truth for the direction. It consolidates the frontend obligations spanning AC1, AC2, AC4, AC6, and the frontend dependencies needed by AC3, AC5, AC7, and AC8. Downstream implementation may land in narrower PM child stories, but the frontend execution and review must trace back here.

### flow.md (verbatim embed)
# User flow — sacrifice on iPhone via Expo Go

## Operator setup flow (once)

1. Operator runs `make mobile-serve` on the host; it prints/persists the Expo
   tunnel URL and QR payload to `logs/expo-go-connection.txt`.
2. Operator installs "Expo Go" from the App Store on the iPhone.
3. Operator scans the QR code with the iPhone camera; Expo Go opens and loads
   the sacrifice app over the tunnel.
4. Operator opens the in-app Diagnostics screen and confirms: resolved API
   URL is the public tunnel URL, backend health shows OK, platform shows iOS.

## Core user journey (must work identically to web)

1. User opens the app in Expo Go → landing screen renders (fonts, styling,
   safe-area correct on a notched iPhone).
2. User registers with email + password → lands authenticated (token stored
   in secure storage; survives app reload).
3. User logs out and logs back in → session restored.
4. User creates a goal via the chat-first creation flow → goal-type options
   reflect backend registry metadata → goal appears on the dashboard.
5. User activates the goal (pledge step) → goal shows active state.
6. User submits proof: taps submit-proof → camera opens (native camera UI,
   permission prompt handled) → captures photo/video OR picks from library →
   upload progress shown → submission accepted (202) → goal shows
   proof-pending state.
7. User revisits dashboard → state consistent with backend (pull-to-refresh
   or reload shows same truth).

## Failure-path expectations

- Backend unreachable (tunnel down): app shows a clear connectivity error —
  not a white screen or infinite spinner; Diagnostics screen shows health
  check failing.
- Camera permission denied: proof flow offers the library-picker fallback
  and explains how to re-enable permission.
- Token expired/invalid: user is routed to login, not stuck on a broken
  authenticated screen.

### api_spec.md
(none)

### Direction acceptance criteria (verbatim embed)
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

### Required context pointers
- [Source: context/project.md#Identity]
- [Source: context/project.md#Stack]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on auth or token lifecycle]
- [Source: context/navigation.md#When working on replay defenses or session invalidation]
- [Source: context/navigation.md#When working on mobile or web login UX]

### Implementation constraints for Dev
- Linux host only; no Xcode, no iOS simulator, no EAS requirement.
- Native verification target is Android emulator + Maestro; iPhone truth is Expo Go over tunnel.
- App code must not hardcode localhost outside the allowed sane localhost default for web dev.
- Web target must remain green; platform divergence belongs behind guards or file splits.
- Host tools (`adb`, emulator, Maestro, `cloudflared`) are operator-provisioned and out of scope.
- Reuse `scripts/smoke_journey.py` semantics for backend boot in mobile E2E-facing work.
- Mobile flows live in `e2e/mobile/`, not a new top-level directory.

### Review checkpoints
- Any remaining `window`, `document`, `localStorage`, or DOM-event assumptions in shared frontend paths are either guarded, split, or removed.
- Backend URL resolution is single-path and inspectable from diagnostics.
- Secure token persistence survives native reload and cleanly falls back on web.
- Proof submission UX is native-first on mobile and web-safe on browser.
- Failure states are user-visible: backend unreachable, camera permission denied, expired token.

## References
- PM tracker: `D086 mobile parity verification and Expo Go deployment`
- Story file path: `stories/210-mobile-parity-native-verification-harness-and-expo-go-deploy.md`
- Flow source: `flow.md`
- Direction source: `direction.md`
- Mobile E2E location constraint: `e2e/mobile/`
- Backend boot semantics reference: `scripts/smoke_journey.py`

## Dev Agent Record
- Status: Not started
- Agent: TBD
- Branch: TBD
- Notes:
  - TBD

## Senior Developer Review
- Reviewer: TBD
- Review date: TBD
- Outcome: Pending
- Notes:
  - Verify strict alignment to AC1-AC8.
  - Verify no frontend divergence breaks web.
  - Verify diagnostics and failure-state UX are observable without debugger.

## Review Follow-ups
- [ ] TBD