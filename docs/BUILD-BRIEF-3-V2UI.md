# Build Brief 3 — EchoTerminal v2: alarms & timers (on-device) + Gemini tools + Spotify plumbing

Repo: echo-gemini. Read docs/HANDOFF.md §8 and docs/BUILD-BRIEF.md first for architecture
context and hard rules (no secrets, single camera owner, never force-stop cameraserver,
commit in slices). Source of truth for behavior: this brief.

## Goal
Make the Show a useful home appliance: a real-time clock with **alarms and timers that
work even when the PC/server is off**, settable by voice through Gemini. Plus protocol
plumbing for Spotify playback (integration itself is a separate spike — see §4).

## 1. Client (EchoTerminal, Kotlin) — alarms & timers, fully on-device

- **UI**: extend MainActivity layer switching (idle/listening/thinking/speaking today).
  Add a **clock-with-tiles home**: big clock, date, connection dot (existing AmbientClock),
  plus an alarms tile and a timers tile. Tap a tile → full-screen Alarms screen / Timers
  screen (reuse the 960×480 large-type style). Back/gesture returns to the clock.
- **Alarms**: local `AlarmManager` (use `setExactAndAllowWhileIdle`; on API 31+ request
  `SCHEDULE_EXACT_ALARM` — `canScheduleExactAlarms()` + prompt fallback). One-time and
  repeating (days-of-week). Persist in SharedPreferences (JSON) so they survive reboot.
- **Timers**: countdowns run in-process; keep CPU awake with a partial wakelock only while
  a timer is active; persist remaining time across process death; fire at zero.
- **Ringing**: when an alarm or timer fires, play a loud local chime via `AudioTrack`
  (synthesize a pleasant 2-tone pattern in code — no asset files), show a full-screen
  "Alarm / Timer done" screen with big Snooze (alarms: +5 min) and Dismiss buttons.
  If the server link is up, ALSO send `alarm_fired` up so Gemini can announce it; the local
  chime must still work with the PC off — never depend on the link for ringing.
- **Voice control path**: server sends `alarm_command` messages down the existing socket
  (`set_alarm`, `set_timer`, `cancel`, `list`). Client applies them to the same local
  scheduler and replies with the resulting state. Wire into the existing message
  multiplexer (see server §2 for schema).
- Keep the existing hard rules: AmbientClock must not import Link; kiosk launcher;
  minSdk 30; armeabi-v7a; no GApps deps; single-owner camera untouched.

## 2. Server (Python) — Gemini tools + protocol

- Extend `server/protocol.py` message schema (keep v1 framing; add message types, bump
  minor version): server→device `alarm_command {op, id?, label?, time_epoch_ms?, days?[],
  duration_s?}`; device→server `alarm_state {alarms:[], timers:[]}` (ack/state) and
  `alarm_fired {kind, id?, label?}`.
- Add **Gemini Live tools** alongside the existing look/stop-look tools (same pattern):
  `set_alarm` (label + "tomorrow 7am" natural time → parse to epoch via LLM/system clock),
  `set_timer` (duration), `cancel_alarm`/`cancel_timer`, `list_alarms`. Tool handler sends
  `alarm_command` to the device, waits for `alarm_state` ack (short timeout), returns the
  confirmed state as the tool response so Gemini can say "Done, alarm set for 7:05".
- When `alarm_fired` arrives while a session is open, inject it as a system prompt note so
  Gemini can react ("Your timer is up").
- Tests for all new tool handlers + protocol round-trips using the existing fake-device
  test harness pattern. Run the full suite: `.venv/Scripts/python.exe -m pytest server/ -q`.

## 3. Deliverables & verification
- Commits in slices (client UI, client scheduler, protocol, server tools, tests).
- Compile the APK: `cd EchoTerminal && export JAVA_HOME="C:/Program Files/Eclipse
  Adoptium/jdk-17.0.20.101-hotspot" && ./gradlew assembleDebug` — must succeed. Report APK
  path + size. (Do NOT claim on-device testing — none possible here.)
- All server tests pass; report count.
- Final report: gates table (what ran + evidence), commit list, what is NOT done, risks.

## 4. Spotify — plumbing ONLY (integration is a later spike)
- Add the display-card type `now_playing` to the display schema (title/artist/album art
  URL/progress) — client WebView renders it like other cards; server has a stub
  `POST /display` example for it.
- Do NOT implement playback. Add a short feasibility note in the repo (`docs/SPOTIFY.md`):
  candidates = librespot with a PCM pipe backend feeding the device socket (needs Premium
  + Windows backend verification), vs device-side Spotify APK (32-bit/1 GB RAM risk), vs
  Spotify Web Player in the WebView (needs Widevine → no GApps → likely dead). Recommend
  the path and list what must be verified. Keep it to ~30 lines.
