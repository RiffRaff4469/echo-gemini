# STATUS — 2026-09-08 (UI-BRIEF-14 client shipped: layout engine home, deployed & device-verified)

_Recorded by Hermes 17:35 ET after the UI-14 client slice build + deploy._

## What shipped (this commit)

### UI-BRIEF-14 client — the layout engine finally changes the screen
Server slice landed earlier (`c5af7e1`, arbitration chat>music>home, 496 tests
green). This commit wires the client to it and rebuilds the WebView home per the
owner home model (docs/HOME-MODEL-OWNER-SPEC.md):

- **65/35 idle home**: clock hero left (never rotates away), widget rail right
  with weather mini, world-clock one-liners (SYR/SIN/GVA), Alarms/Timers/
  Stopwatch chips, mute chip (only while muted), now-playing chip (only while a
  speaker card is up), hint line. Idle page-cycling is gone — full pages
  (weather, world, scores/flights/news/stocks dormant) open over the engine on
  tile tap and return via a `< Home` chip. Blank-screen taps do nothing (v1.6).
- **Focus templates**: `layout {focus}` messages drive body classes. `chat`
  shows the conversation template (dim + pill + compact corner clock; answers
  stay the full-bleed card hero); `music` is **home + now-playing chip** — music
  no longer takes over the screen. Chat→idle returns instantly to the home.
- **Now-playing while idle renders as the rail chip** (title · artist, eq bars,
  paused state), not a hijacking fullscreen card; `display_clear` removes it.
- Transitions are opacity-only ≤ 400 ms (convo fade, page-in); no layout-thrash
  animations — the device is 1 GB / 32-bit.

### Kotlin (small, surgical)
- `Protocol.kt`: `Type.LAYOUT` (v1.6; comment updated).
- `AmbientWeb`: `layout(focus)` (sanitizes to home|music|chat) and `mute(on)`
  push surface; JS→native bridge comment corrected (six calls, incl.
  openStopwatch).
- `MainActivity`: consumes `layout` and forwards to the WebView; privacy-mute
  transitions now push `Echo.mute` so the rail chip mirrors the device gate
  (calls queue until the page is ready, so a muted boot still shows the chip).

### Verification (real, on-device)
- `pytest`: 496 passed (server untouched).
- `node --check` + headless-Chrome load of the page: no console errors.
- Built APK, `adb install -r`, drove the physical device over adb; each item
  confirmed by screenshot/OCR + logcat + uiautomator:
  1. Boot → 65/35 home renders (clock 5:19 + date; weather "Partly cloudy";
     SYR 5:19 PM / SIN 5:19 AM / GVA 11:19 PM; chips; hint).
  2. Mic button (F1) → server `state listening` + `layout chat` → chat template
     (compact corner clock + "Listening" pill); F1 again → home returns.
  3. Blank tap → "tap ignored: screen taps are UI-only since v1.6".
  4. Rail weather tile → Weather page; `< Home` chip → back to engine.
  5. Alarm chip → native Alarms page ("No alarms set"); Stopwatch chip → native
     Stopwatch page (00:00.0 + Start/Lap/Reset).
  6. F1 hold ≥ 1 s (sendevent) → "privacy MUTE on" + "Mic muted" chip in rail;
     hold again → chip gone.
  7. `now_playing` display push while idle → np chip on the home (clock stays
     visible); `/display/clear` → chip gone.
  8. `text` answer push still renders fullscreen and self-clears (3 s).
- One bug caught by the device run and fixed in this commit: `cardShowing` was
  used but never declared (ReferenceError on every card push) — added the
  declaration; re-verified 2–8 after the fix.

## Device state
- App installed: debug build, `com.echogemini.terminal.debug`, protocol v1.6.
- Server running on :8766 (untouched), device connected, screen resting on the
  65/35 home, unmuted.

## Owner verify list (3 steps)
1. **Spotify voice**: "hey jarvis, play some lo-fi" — music plays; the home
   keeps the clock with a now-playing chip in the rail (no fullscreen takeover).
2. **Stopwatch voice + screen**: "start a stopwatch" (voice) and tap the
   Stopwatch chip — native page shows the running time; it survives a restart.
3. **Mute persistence**: hold the mic button ≥ 1 s (chip shows "Mic muted"),
   reboot the device, confirm it boots still muted (chip still shown).

## Next up
- UI-BRIEF-15 (music template with transport) after the owner verify round.
- Rail rotation after long idle (client-side, home-model rule 3) is parked:
  the rail is a static stack today; the clock never rotates away either way.
