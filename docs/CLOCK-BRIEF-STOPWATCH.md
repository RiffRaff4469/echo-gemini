# CLOCK-BRIEF-STOPWATCH — Clock suite: alarm / timer / stopwatch pages, UI + Gemini

Repo: echo-gemini. Read `docs/HANDOFF.md` and `docs/BUILD-BRIEF-3-V2UI.md` first
(v2 alarms/timers architecture + hard rules). Part of the owner's Nest-Hub UI
overhaul plan (ROADMAP.md); sibling briefs: `HARDWARE-BRIEF-7-PHYSICAL-CONTROLS.md`
(v2 — screen taps are UI-only; this brief's tiles are UI taps), `UI-BRIEF-14-*`,
`UI-BRIEF-15-*`, `UI-BRIEF-16-*`.

## Goal

Make the Show a complete clock appliance: **alarms, timers, and a NEW stopwatch** —
each a home tile that opens a full-screen page, each fully controllable by
on-screen UI **and** by Gemini voice. Alarms/timers keep their deliberate
**on-device, PC-off** semantics (they already do); the stopwatch follows the same
pattern. A stopwatch must keep counting with the PC asleep.

## Current state (shipped in v2, `ad4d2e8` — read the code, don't rebuild it)

- `AlarmScheduler` / `AlarmService` / `ScheduleScreen` (native, custom views, no
  Compose); exact-alarm permission + boot receiver; alarms/timers persist in
  SharedPreferences; chime synthesized via AudioTrack; fire screens with
  Snooze/Dismiss.
- Home entry points exist: native tiles + WebView chips → `MainActivity` alarms/timers
  entry (see MainActivity around the alarms/timers entry point).
- Server: `alarm_command {set_alarm|set_timer|cancel|list}` (v1.1) + `alarm_state` /
  `alarm_fired` + Gemini tools; server is a convenience — device applies ops locally
  and replies with state.

## Client work (EchoTerminal)

1. **Stopwatch service** (mirror the timers pattern):
   - count-up with lap support; partial wakelock only while running; elapsed persists
     across process death (SharedPreferences); pause/resume; reset.
   - display: large `MM:SS` (small tenths) at 960×480 large-type scale; running state
     survives WebView/activity re-creation (state in the service, view is a mirror).
   - no fire-chime (it's a stopwatch); optional lap announcement is out of scope.
2. **Stopwatch page**: full-screen, big touch targets (≥ 40 px rows): Start/Pause,
   Lap, Reset; lap list (latest N, scroll if long). Back → home clock.
3. **Home entry**: stopwatch tile + WebView chip alongside the existing alarm/timer
   ones (native + WebView bridges already exist for chips; add the stopwatch one).
4. **UI CRUD parity audit** (alarms/timers): voice can create/edit/cancel — the
   on-screen screens must match: create alarm (time + repeat days), create timer,
   cancel/delete, snooze/dismiss on fire. Close any gaps found; don't rebuild what
   works. (This is the "controllable by UI" half of the requirement.)
5. Style: keep the existing large-type language; a visual-tokens restyle pass comes
   with UI-BRIEF-14 — do not block on it, but keep markup/classes simple to restyle.

## Server work (Python)

1. **Protocol (v1.6, keep v1 framing):** extend the `alarm_command` op set with
   `stopwatch_start`, `stopwatch_pause` (aka stop), `stopwatch_reset`,
   `stopwatch_lap`, `stopwatch_status`. Add device → server
   `stopwatch_state {running: bool, elapsed_ms: int, laps_ms: [int]}` pushed on every
   transition (start/pause/reset/lap) — NOT ticked every second. `stopwatch_status`
   op gets a fresh round-trip answer for "what's the stopwatch at?".
2. **Gemini tools** (same pattern as the alarm tools — send op, wait ack, return
   confirmed state so Gemini can answer with a fact): `start_stopwatch`,
   `stop_stopwatch`, `lap_stopwatch`, `reset_stopwatch`, `stopwatch_status`.
3. `alarm_fired`-style handling is NOT needed for the stopwatch (nothing fires).
4. Tests: protocol round-trips, all tool handlers against the fake-device pattern,
   transition state machine (running→pause persists elapsed; lap appends; reset
   clears). Full suite green.

## Scope rules

- On-device = source of truth for clock state (PC-off guarantee). Server never
  schedules locally — it relays voice intents and mirrors state.
- No Compose; armeabi-v7a; minSdk 30; don't touch camera/audio pipeline or the
  WebView ambient layout engine (UI-BRIEF-14's territory).
- One worker per repo. Commit in slices (service, page/tiles, protocol, tools, tests).

## Deliverable + verification

- Server suite green: `.venv/Scripts/python.exe -m pytest server/ -q`.
- APK compiles: `cd EchoTerminal && export JAVA_HOME="C:/Program Files/Eclipse
  Adoptium/jdk-17.0.20.101-hotspot" && ./gradlew assembleDebug --no-daemon`.
- On-device: start/pause/lap/reset stopwatch from the screen AND by voice ("start a
  stopwatch", "what's the stopwatch at?"); set + dismiss an alarm and timer from the
  UI alone (no voice); **PC/server off**: stopwatch keeps counting and an alarm still
  rings (kill the server, verify both).
- Report ~15 lines + commit SHAs; state explicitly any UI CRUD gaps found and closed.
