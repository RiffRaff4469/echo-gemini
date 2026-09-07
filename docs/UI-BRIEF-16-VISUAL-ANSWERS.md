# UI-BRIEF-16 — Visual answers: interactive options/lists + tap-to-choose

Repo: echo-gemini. Read `docs/HANDOFF.md` (§8.1 display), `docs/UI-BRIEF-9-AMBIENT-
WEBVIEW.md` (card renderer), `docs/UI-BRIEF-14-LAYOUT-ENGINE.md` (chat template +
UI-only screen) and `docs/HARDWARE-BRIEF-7-PHYSICAL-CONTROLS.md` v2 (screen taps =
UI only — this brief's whole premise) first.

**Prerequisites:** HARDWARE-BRIEF-7 v2 (no tap-to-talk conflicts) + UI-BRIEF-14
(chat template mounts panels). Independent of 15.

## Goal

Gemini answers can drive **typed, interactive visual panels** — the "trivia shows the
options on screen and you tap one" case. Panels are structured display types rendered
as TRUSTED DOM by the device's own renderer (same trust class as text/timer/now_playing
cards) — NOT server HTML, no new sandbox surface.

## 1. New display types (protocol v1.6; `server/protocol.py` DisplayType)

- `options` payload: `{title?, question?, options: [{label, sub?}]}` — 2–6 options
  (reject > 6: touch fit at 960×480; labels ≤ ~28 chars, server truncates/errors).
- `list` payload: `{title?, items: [str]}` — enumerated results (steps, recipes,
  schedules, search results).
- Both push as normal DisplayCommands (high priority, duration 0 = until resolved/
  cleared). Server keeps **panel state** (current panel + question + option labels)
  so taps can be mapped back to labels; cleared on: next panel, display_clear, voice
  cancel, session idle timeout.

## 2. Device rendering (WebView)

- Extend the card renderer in `ambient/app.js`: `options` → a card of big tappable
  rows (≥ 48 px, focus/active states, selected highlight), `list` → scannable items
  with a compact header. Mount in the chat template from UI-BRIEF-14 (hero position).
- Row tap → new device→server message `select {index}` (0-based) — deliberately NOT
  a `tap`/`button` message; talk is owned by the mic button and wake word.
- Canvas fallback (no WebView): options/list degrade to a plain text card
  (non-interactive) — acceptable, fallback is emergency-only.

## 3. Server + Gemini

- Gemini tools (pattern-match the alarm/spotify tools): `show_options {question,
  options[]}` and `show_list {title, items[]}`. Tool handler: store panel state →
  push the DisplayCommand → return immediately (speech continues; the model says the
  question in one line and lets the screen hold the detail — do NOT read all options
  aloud).
- `select` handling (the tricky bit — spec it in tests):
  - panel showing + **session active** → queue the selection; apply it when the
    current turn ends (inject "the user chose <label>" as the next input so the model
    responds naturally);
  - panel showing + **idle** → open a session with context ("on-screen question:
    <question>; user tapped <label>") — the model continues from there;
  - no panel → ignore.
- Prompt: update `prompts/jarvis-system.txt` (committed, prompt-driven): use
  `show_options` for any question with discrete choices (trivia, "which do you
  want", yes/no-ish menus); use `show_list` for enumerations; keep the spoken reply
  to one short sentence + let the screen carry the detail. Also: a `clear_panel`
  path when the user says "never mind"/"clear the screen".

## 4. Tests

- Tool→display push mapping; > 6 options rejected; select-during-active-session
  queueing; select-while-idle opens session with context; panel cleared on next
  panel/display_clear/voice-cancel; fake-device end-to-end: question → options on
  screen → tap index 2 → model receives the chosen label.

## Scope rules

- No server-HTML rendering for these (typed payloads only). No Compose. Don't change
  the wake/button model. One worker per repo; commit in slices (protocol, renderer,
  tools+prompt, tests).

## Deliverable + verification

- `pytest server/ -q` green (full suite + new e2e); `./gradlew assembleDebug` green
  (JAVA_HOME Adoptium 17).
- On-device: "give me a trivia question" → question spoken + 4 option rows; tap the
  right one → Jarvis reacts to the choice; a second question replaces the panel; "hey
  jarvis, list my alarms" → list panel while the voice says the summary; screen blank
  tap does nothing; mic-button still owns talk.
- Report ~15 lines + commit SHAs.
