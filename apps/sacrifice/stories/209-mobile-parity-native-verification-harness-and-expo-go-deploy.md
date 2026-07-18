# Story
**Title:** Mobile parity, native verification harness, and Expo Go deployment to iPhone — narrow read
**Slug:** mobile-parity-native-verification-harness-and-expo-go-deploy
**Scope:** frontend

## Acceptance Criteria
1. **Parity audit is executed and codified**: an automated check (script + test) inventories web-only API usage in shared code paths (`document.`, `window.` outside platform guards, `localStorage`, DOM event types) and the core flows (register/login, goal creation including registry metadata, proof capture/upload, dashboard, chat-first creation) each have a native-compatible implementation. The check runs in CI/test gate and fails on new unguarded web-only API usage.
2. **API base URL is configuration**: the app resolves its backend from `EXPO_PUBLIC_API_URL` (with a sane localhost default for web dev). All fetch/auth/upload call sites go through one client module that honors it. Token storage uses `expo-secure-store` on native and falls back cleanly on web.
3. **Native E2E harness exists and is green**: `make mobile-e2e` (repo root) boots the backend (isolated port, same pattern as `make smoke`), launches the app on the Android emulator, and drives the core journey (register → login → create goal → activate → submit proof) via Maestro flows checked into `e2e/mobile/`. Exits non-zero on any step failure.
4. **Camera/media proof path works natively**: proof capture uses `expo-camera`/media-library on native (not browser file inputs), uploads succeed against the backend, and the Maestro flow covers a submit-proof-with-media step (emulator virtual camera acceptable).
5. **Expo Go serving is a service**: `make mobile-serve` starts `expo start --tunnel` non-interactively, writes the connection URL + QR payload to `logs/expo-go-connection.txt`, and stays healthy in the background (documented systemd user unit or equivalent). `make mobile-serve-status` reports whether the tunnel and Metro bundler are up.
6. **On-device diagnostics screen**: a lightweight in-app screen (dev builds only) shows the resolved API URL, backend `/api/health` status, platform/OS, and app version — so a human holding the phone can verify connectivity in one glance without a debugger.
7. **iPhone runbook**: `context/mobile-runbook.md` documents the full operator path — start services, scan QR with iPhone, install Expo Go, verify via the diagnostics screen, run through the core journey — plus troubleshooting (tunnel down, Metro cache, LAN vs tunnel mode).
8. **Full-journey native verification against the tunnel**: the Maestro core-journey flow passes on the Android emulator with the app pointed at the PUBLIC tunnel URL (not localhost), proving the same path an iPhone will take end-to-end.

### Testable Claims (EARS)
AC1.1: WHEN the parity audit is executed, THE audit tooling SHALL inventory web-only API usage in shared code paths for `document.`, `window.` outside platform guards, `localStorage`, and DOM event types.
AC1.2: WHEN core flows are exercised in native runtime, THE frontend implementation SHALL provide native-compatible behavior for register/login, goal creation including registry metadata, proof capture/upload, dashboard, and chat-first creation.
AC1.3: WHEN new unguarded web-only API usage is introduced in shared code paths, THE CI/test gate SHALL fail.
AC2.1: WHEN the app resolves its backend base URL, THE frontend client SHALL read `EXPO_PUBLIC_API_URL` as configuration.
AC2.2: WHEN the app runs in web development without explicit configuration, THE frontend client SHALL use a sane localhost default.
AC2.3: WHEN fetch, auth, or upload requests are made, THE app SHALL route those call sites through one client module that honors the configured backend URL.
AC2.4: WHEN token storage is used on native platforms, THE app SHALL use `expo-secure-store`.
AC2.5: WHEN token storage is used on web, THE app SHALL fall back cleanly.
AC3.1: WHEN `make mobile-e2e` is run, THE repo tooling SHALL boot the backend on an isolated port using the same pattern as `make smoke`.
AC3.2: WHEN `make mobile-e2e` is run, THE repo tooling SHALL launch the app on the Android emulator.
AC3.3: WHEN `make mobile-e2e` is run, THE Maestro flows under `e2e/mobile/` SHALL drive the core journey of register, login, create goal, activate, and submit proof.
AC3.4: WHEN any mobile E2E step fails, THE `make mobile-e2e` command SHALL exit non-zero.
AC4.1: WHEN proof capture is initiated on native platforms, THE app SHALL use `expo-camera` and media-library rather than browser file inputs.
AC4.2: WHEN native proof media is submitted, THE upload SHALL succeed against the backend.
AC4.3: WHEN the Maestro proof flow is run, THE flow SHALL cover a submit-proof-with-media step.
AC5.1: WHEN `make mobile-serve` is run, THE repo tooling SHALL start `expo start --tunnel` non-interactively.
AC5.2: WHEN `make mobile-serve` starts successfully, THE repo tooling SHALL write the connection URL and QR payload to `logs/expo-go-connection.txt`.
AC5.3: WHEN the Expo Go serving service is running, THE service SHALL stay healthy in the background.
AC5.4: WHEN `make mobile-serve-status` is run, THE repo tooling SHALL report whether the tunnel and Metro bundler are up.
AC6.1: WHEN a dev build displays the diagnostics screen, THE screen SHALL show the resolved API URL.
AC6.2: WHEN a dev build displays the diagnostics screen, THE screen SHALL show backend `/api/health` status.
AC6.3: WHEN a dev build displays the diagnostics screen, THE screen SHALL show platform and OS.
AC6.4: WHEN a dev build displays the diagnostics screen, THE screen SHALL show app version.
AC6.5: WHEN the app is not a dev build, THE diagnostics screen SHALL be unavailable.
AC7.1: WHEN an operator reads `context/mobile-runbook.md`, THE document SHALL describe the full operator path of starting services, scanning the QR with iPhone, installing Expo Go, verifying via diagnostics, and running the core journey.
AC7.2: WHEN an operator reads `context/mobile-runbook.md`, THE document SHALL include troubleshooting for tunnel down, Metro cache, and LAN vs tunnel mode.
AC8.1: WHEN the Maestro core-journey flow is run on the Android emulator, GIVEN the app is pointed at the public tunnel URL, THE flow SHALL pass end-to-end.
AC8.2: WHEN the app is configured for tunnel verification, THE app SHALL use the public tunnel URL rather than localhost.

## Tasks / Subtasks
- [ ] Centralize frontend backend URL resolution in one client/config module.
- [ ] Replace hardcoded localhost usage in app code with client/config consumption.
- [ ] Route fetch/auth/upload call sites through the shared client module.
- [ ] Implement platform-aware token storage adapter for native/web parity.
- [ ] Add frontend-native-safe platform guards or file splits for shared code paths.
- [ ] Remove or isolate unguarded web-only API usage from shared/native paths.
- [ ] Update goal creation flow to consume backend goal metadata rather than hardcoded local goal-type constants where native parity depends on it.
- [ ] Add native proof capture UI path using Expo-native APIs.
- [ ] Preserve web-safe behavior behind guards or `.web` splits.
- [ ] Add dev-only diagnostics screen showing resolved API URL, `/api/health`, platform/OS, and app version.
- [ ] Ensure diagnostics screen is excluded from non-dev builds.
- [ ] Verify authenticated flows survive native reload via secure token storage.
- [ ] Confirm dashboard and chat-first creation render correctly in native runtime.
- [ ] Coordinate with backend/test/infra stories for upload transport, Maestro harness, tunnel serving, and runbook dependencies.

## Dev Notes
### Scope intent
Frontend-only narrow read: prepare the mobile-native parity implementation surface in app code. Infra harness, backend upload contract changes, and docs outputs remain external dependencies even though direction ACs are embedded here for traceability.

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
[api_spec.md: none]

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

### Context pointers
- [Source: context/project.md#Identity]
- [Source: context/project.md#Stack]
- [Source: context/project.md#Active constraints]
- [Source: context/navigation.md#When working on mobile goal creation or proof UX]
- [Source: context/navigation.md#When working on camera-based or uploaded proofs]
- [Source: context/current-state.md#frontend]
- [Source: context/modules/frontend.md#Goal creation]
- [Source: context/modules/frontend.md#API and auth]
- [Source: context/modules/frontend.md#Proof submission]
- [Source: context/modules/backend.md#Goal types]

### Implementation constraints
- Linux host only; no iOS simulator assumptions in code paths.
- iPhone runtime is Expo Go over tunnel; backend URL must be configurable.
- Preserve web target behavior; use `Platform.OS` guards or `.native` / `.web` splits.
- Do not introduce app-code hardcoded localhost outside the shared config module.
- Native-proof UI must not depend on browser file inputs.
- If upload transport requires multipart/form-data or backend contract changes, treat that as a dependency on the backend story; frontend should align to the agreed client seam without inventing the server contract.
- If parity audit gate lives outside frontend code, still remove/isolate violations in frontend shared paths so the external gate passes.

### Dependencies / external story handoffs
- Backend story dependency: `D086 add proof media upload transport for native clients` for server acceptance of native media uploads.
- Test story dependency: `D086 add parity audit script and test for web-only API usage` for CI/test gate implementation.
- Infra story dependency: `D086 add make mobile-e2e and Maestro smoke plumbing` and `D086 add make mobile-serve and mobile-serve-status wrappers` for runtime verification surfaces.
- Docs story dependency: `D086 document iPhone Expo Go operator runbook` for `context/mobile-runbook.md`.

## References
- `frontend/App.tsx`
- `frontend/screens/GoalCreateScreen.tsx`
- `frontend/services/api.ts`
- `backend/app/routes/goals.py`
- `backend/app/goal_types/registry.py`
- `backend/app/schemas/goal.py`
- `backend/app/models/goal.py`
- `frontend/app.json`
- `scripts/smoke_journey.py`

## Dev Agent Record
- Status: Not started
- Open questions: None; use direction + context as source of truth.
- Blockers:
  - Backend upload contract may need expansion beyond current JSON-only proof submission.
  - Diagnostics screen route placement must fit existing frontend navigation structure.

## Senior Developer Review
- Pending implementation.

## Review Follow-ups
- None yet.
