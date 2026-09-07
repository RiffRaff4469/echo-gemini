# FIX-BRIEF-11 — Tap = END the conversation (replaces brief-5 "stay")

Owner change (2026-09-06, after using it): tapping the screen while Jarvis is in
a session should **end the conversation immediately**, not extend it.

## Current behavior (brief 5, protocol v1.3)
- IDLE tap → `tap` (tap-to-talk, opens a session)
- Non-IDLE tap → `stay` (+30 s quiet window)

## New behavior (protocol v1.4)
- IDLE tap → `tap` (unchanged)
- **Non-IDLE tap → `stop`**: immediately close the active session (same path as
  the watchdog/quiet-window close — clean teardown, back to ambient, wake word
  re-arms). Mid-answer taps cut the answer off. This is intentional.

## Changes
- `server/protocol.py`: MINOR → 4. Add device→server `stop` (no fields). Keep
  `stay` in the parser for one release (older clients) but treat it as `stop`
  (owner semantics supersede) — or remove cleanly if trivially safe; prefer
  keep-and-map to stop.
- `server/main.py`: `_on_stop` → close the live session via the same close path
  the quiet-window uses (not a hard kill — graceful: stop streaming, close Live
  session, notify device → ambient). `stay` handler → same behavior.
- Client (`Protocol.kt` MINOR 4, `Link.kt` sendStop, `MainActivity.kt` tap
  dispatch): non-IDLE tap sends `stop`. The brief-5 "last 3 s" hint text
  (`session_quiet`) should now say something like "tap to stop" — or simply be
  removed if it reads oddly; owner's mental model is tap-anytime = stop.
- WebView ambient bridge taps must route through the same dispatch (non-IDLE tap
  → stop), consistent with MainActivity.
- Tests: server tests for `stop` (idle vs active, mid-answer, race with close);
  update the 13 brief-5 tests that assert `stay`.

## Verification
`pytest server/ -q` green; `./gradlew testDebugUnitTest assembleDebug` green.
Do NOT deploy/push to the device — Hermes handles install + server restart.
