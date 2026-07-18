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
