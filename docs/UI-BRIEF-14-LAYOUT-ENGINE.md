# UI-BRIEF-14 — Layout engine + Nest-Hub home (65/35 clock→widgets); screen = UI only

Repo: echo-gemini. Read `docs/HANDOFF.md` (esp. §8.1 display, §8.2 uiState) and
`docs/UI-BRIEF-9-AMBIENT-WEBVIEW.md` (the ambient layer this rebuilds on) first.
Owner's Nest-Hub overhaul, brief 3 of the queue (ROADMAP.md).

**Prerequisite: HARDWARE-BRIEF-7 v2 must be landed** — screen taps no longer start or
stop chats (button + wake word own talk). This brief turns the freed-up screen into a
real UI. Do not dispatch before that lands (the old tap semantics fight every
interaction here).

## Goal

Replace the "static home page + cards that cover it" model with a **focus-driven
layout engine**: one screen whose arrangement follows what is happening, plus a
Nest-Hub-style idle home: **clock hero ~65% left, widget rail ~35% right**. Everything
renders in the existing WebView ambient (`EchoTerminal/app/src/main/assets/ambient/`),
vanilla JS, no frameworks.

## 1. Focus model

- Server computes and pushes a `layout {focus}` message (protocol v1.6, keep v1
  framing): focus ∈ `home` | `music` | `chat`. Rules (single source of truth,
  server-side, unit-tested):
  - uiState != idle → `chat`;
  - media playing or paused → `music` (full spec in UI-BRIEF-15 — 14 ships the
    template + a stub data path, 15 fills it);
  - otherwise → `home`.
  - Full-screen pages (alarms/timers/stopwatch, weather, world clock) open OVER the
    engine as today — focus returns when they close.
- Client keeps a small focus→template map; a focus change re-arranges the SAME DOM via
  CSS grid areas. Transitions: opacity + transform only, ≤ 400 ms — GPU-cheap; the
  device is 1 GB / 32-bit and the WebView is the only renderer. No layout-thrash
  animations, no always-on motion.

## 2. Home template (idle focus) — the 65/35

- **Left 65%:** clock hero (existing dot/digital faces, date). Big. Glanceable.
- **Right 35% widget rail** (stacked, from ALREADY-pushed data only — no new feeds;
  UI-BRIEF-10 later grows the gallery with scores/flights/news):
  1. Weather mini (weather push exists);
  2. World clocks — compact one-liners SYD / SIN / GVA (world.js exists);
  3. Clock-suite status chips: alarms count, timers count, stopwatch state (schedule
     counts are pushed; stopwatch state comes with CLOCK-BRIEF-STOPWATCH);
  4. Mute status chip (HARDWARE-BRIEF-7 v2 mute state; hidden when unmuted);
  5. Hint line replacing "Tap to talk" (interim copy from brief 7: "say hey jarvis ·
     press mic").
- Chips/tiles are ≥ 40 px targets; tapping one opens its page (local navigation —
  no server round trip needed to open a page).

## 3. Chat template (non-idle focus)

- Answer/content card becomes the hero (existing pushed-card machinery), clock
  compresses to a compact corner block, ring/listening visuals unchanged. Cards that
  arrive during chat are the hero; on session end → focus returns to home (or music,
  per arbitration above).
- Interactive panels (option rows from UI-BRIEF-16) mount in this template.

## 4. Interaction changes (client + server)

- WebView: blank-tap does nothing (no chat start). Chip/widget taps open pages
  locally. The old document-click → `EchoNative.tap()` → chat path is gone (brief 7
  removed server-side handling; this brief removes the last UI remnants).
- MainActivity: no session starts from screen touch; taps forward as UI semantics
  only. Canvas fallback (no-WebView) keeps showing the clock — no widget rail there,
  that's fine (fallback only).

## 5. Server work

- `layout` message + focus computation on state/media changes (watch uiState
  transitions, media state events, display pushes). Tests for the arbitration table.
- No new data pipelines. Reuse existing pushes (weather/schedule/pageData).

## Files

- `EchoTerminal/app/src/main/assets/ambient/index.html` + `app.js` (structure,
  templates, transitions; CSS lives with them),
- `server/protocol.py` (layout msg), `server/main.py` (focus computation),
- MainActivity WebView bridge surface (layout + UI-tap plumbing),
- tests: `server/tests/` focus arbitration + fake-device layout flow.

## Scope rules

- No frameworks, no new assets of weight; keep the sandboxed-html card path intact
  (it overlays the engine as today). Don't touch native ScheduleScreen pages beyond
  entry chips. One worker per repo; commit in slices (protocol, server focus, client
  templates, tests).

## Deliverable + verification

- `pytest server/ -q` green; `./gradlew assembleDebug` green (JAVA_HOME Adoptium 17,
  ANDROID_HOME `C:\Users\jaide\Android\Sdk`).
- On-device (wireless adb): idle shows 65/35 home; transitions are smooth on the
  1 GB device (watch for jank on focus flips; logcat frame drops if suspicious);
  "hey jarvis" → chat focus → answer → back home; mic-button press starts a chat
  (brief 7 wiring); a screen tap on empty space does NOTHING; tapping the weather
  mini opens the weather page; tapping alarm chip opens alarms.
- Report ~15 lines + commit SHAs + any perf observations.
