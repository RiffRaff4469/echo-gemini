# UI-BRIEF-9 — Ambient UI on WebView (owner-approved pivot, 2026-09-06)

The owner approved replacing the Canvas ambient screen with a **WebView-hosted
UI**, porting `docs/ui-mockup/mockup.html` (design spec + handover in
`docs/ui-mockup/HANDOVER.md`). Rationale: the mockup is zero-dependency
HTML/CSS/JS; porting is direct; UI iteration becomes asset edits without APK
rebuilds; iPhone-StandBy styling and page auto-cycling are trivial in JS.

## Architecture (what to build)
- EchoTerminal's display layer becomes a full-screen `WebView` hosting the
  ported ambient UI (assets bundled in the APK `assets/` — device has NO direct
  internet; fonts must be bundled or removed → bundle Space Grotesk + Inter
  WOFF2 subsets into assets, or fall back to system sans for v1 — prefer
  bundled fonts, they sell the look).
- The native core keeps ALL plumbing: `Link.kt` WS connection, audio
  capture/playback, `CameraSource`, `AlarmScheduler`/`AlarmService`,
  tap-to-talk, permissions, boot receiver. WebView replaces ONLY what is
  displayed (AmbientClock/StatusOverlay/PushSurface rendering paths).
- **JS bridge** (native → JS via `evaluateJavascript`, JS → native via
  `addJavascriptInterface` with a narrow, audited surface — no raw shell):
  - native→JS: `linkState(online|connecting|offline)` → the color-only dot;
    `uiState(idle|listening|thinking|speaking)` → listening ring pulse + page
    visibility (idle only = auto-cycle; any non-idle = conversation overlay
    mode, cycling stops); `weather({temp_c, code, is_day})` (reuse the EXISTING
    protocol v1.2 weather push — do not redesign it); `display(type,payload)`
    for the existing text/html/image/timer/now_playing server cards (render as
    an overlay card in JS, same behavior as today's PushSurface); `time` isn't
    needed (JS clock uses device time).
  - JS→native: `tap()` (tap-to-talk, same semantics as today — screen tap while
    connected sends tap unless a page interaction consumed it);
    `openAlarm()` / `openTimer()` (open the existing native schedule screens);
    `stay()` (the chat-end "tap to keep talking" signal from UI-BRIEF-5 if that
    landed — coordinate with its wire name).
- **Idle auto-cycle (owner spec):** when `uiState=idle`, rotate pages every
  ~15 s (home clock → weather → world clock → scores → flights → news → stocks
  → home…). Rules: skip pages whose data is absent/placeholder (v1: only
  home/weather/world-clock have real data — cycle those; others render when
  BRIEF-10 data lands); any conversation state (listening/thinking/speaking)
  instantly stops cycling and shows the conversation overlay; a manual tap on a
  page dot pins that page (resume cycling after ~60 s idle on it).
- **World clock:** client-computed (the terminator math is already in the
  mockup's JS). Cities: **Syracuse, Singapore, Geneva** (replace the mockup's
  Madrid/Rome). City list as a small JS config (later editable without APK
  rebuild via a config payload — v1 hardcode in the asset).
- **Clock:** ONE big StandBy-style digital face as the default (Space Grotesk,
  tabular nums, ~120 px). The face-switcher pills and analog/bold/retro faces
  are NOT required for v1 — drop or hide them (keep code paths out; simplest
  v1 = digital only, mockup otherwise intact).
- **Scores/flights/news/stocks pages:** port the layout/markup EXACTLY as the
  mockup but bind rows to a `pageData(page,json)` bridge payload with a
  "no data yet" placeholder state (greyed sample rows or an empty state).
  Real data arrives in BRIEF-10 (server fetchers + pushes). Do not fake
  "live" data — placeholder must read as placeholder.

## Fallback requirement (do not skip)
The custom ROM being built has WebView REMOVED (pdk-sync casualty). If the
WebView cannot initialize (missing system webview / crash on load), the app
MUST fall back to the existing Canvas ambient (UI-BRIEF-4 code paths stay
compiled in) with a logged reason. Test this path by simulating webview
absence if feasible; at minimum keep the branch clean and note it.

## Constraints
- Views/WebView + Kotlin only — NO Compose. armeabi-v7a, minSdk 30.
- Do not break: link/reconnect, alarms/timers/schedule screens (native),
  audio/camera, tap-to-talk semantics, wake word flow, server protocol
  (v1.2 weather/state unchanged — no server edits unless a tiny gap is found,
  say so in the report).
- WSL / ROM build untouched. One-worker repo rule: sequence after whatever
  brief is currently running.
- Assets live in `EchoTerminal/app/src/main/assets/ambient/`; the ported UI
  must not reference network fonts/CDNs — everything local.
- RAM: keep the WebView lean (no hardware-accel debug layers, no extra JS
  frameworks, no polling timers when the screen shows a conversation).

## Deliverable + verification
- Commits with clear messages (client only, unless a protocol gap is found).
- `./gradlew assembleDebug` green (JAVA_HOME
  `C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot`, ANDROID_HOME
  `C:\Users\jaide\Android\Sdk`). Server suite untouched but run once to prove
  no accidental breakage (`.venv/Scripts/python.exe -m pytest server/ -q`).
- Live on-device check (wireless adb `100.106.97.46:5555`): ambient renders
  the ported UI; weather card shows Syracuse temp; dot tracks link state;
  listening ring pulses during a chat; cycling runs when idle and stops during
  conversation; world clock shows Syracuse/Singapore/Geneva with correct times
  and a sane terminator. Screenshot + ~20-line report + commit SHAs. Do not
  claim done without the on-device pass.
