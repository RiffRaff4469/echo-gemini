# UI-BRIEF-4-AMBIENT — Ambient Home Screen (Echo-Show-grade idle UI)

Goal: redesign EchoTerminal's IDLE/AMBIENT screen from the current functional
skeleton (black canvas + plain clock text + raw status text) into something that
looks as considered as an Amazon Echo Show home screen. Read-only reference:
`docs/HANDOFF.md` architecture + `docs/BUILD-BRIEF-3-V2UI.md` for the v2 UI
conventions. READ the actual current code first — `AmbientClock.kt`,
`MainActivity.kt`, `StatusOverlay.kt`, `Protocol.kt`, `Link.kt` — the redesign
must plug into the existing View/Canvas architecture, NOT replace it.

## Scope (this pass)
The idle/ambient home screen only — the screen shown when there is no active
conversation and no alarm/timer/schedule flow is open:
1. **Time-of-day ambient gradient background** (full-bleed, slowly shifting)
2. **Big thin clock + date** (readable across the room on a 960x480 landscape panel)
3. **Weather card** (server-pushed; Open-Meteo via the echo-server — the device
   has NO direct internet by design, everything comes over the tailnet WS link)
4. **Refined link/status dot** (keep the existing semantics: green/amber/grey for
   link state — make it a subtle glowing dot, not a raw colored circle)
5. Tap-to-talk affordance and the top conversation band (LISTENING/THINKING/
   SPEAKING) keep their existing behavior/labels but may be restyled minimally so
   they don't look bolted-on.

## Design direction (deliver this feel)
Echo Show ambient, not a phone clock app: calm, dark-first, high contrast,
glanceable from 2 m away, zero clutter. Reference palette anchors:
- Night (00:00–06:30): deep navy `#0A0E1F` → `#131A33`
- Dawn (06:30–09:00): `#131A33` → muted indigo/peach horizon (`#2A2342` → `#4A3B52`)
- Day (09:00–17:00): soft slate-blue `#1C2740` → `#24365C` (keep it dark-first —
  this is an always-on display, avoid bright whites)
- Dusk (17:00–20:30): `#24365C` → violet `#3A2C4A` → ember `#5A3A3A`
- Transitions blend continuously with local device time (interpolate the color
  stops; no hard cuts). A slow, subtle motion (e.g. a drifting soft glow/aurora
  overlay or very slow gradient shift) is welcome but must be battery/CPU cheap —
  1 GB device, always-on. No heavy shaders, no per-frame allocations.
- Clock: very large, thin/light weight (system font, light), white with soft
  shadow or subtle alpha layering so it reads over any gradient; date + weekday
  in a smaller muted line. Center or upper-center layout that leaves room for a
  future status line.
- Weather card: small rounded card (rounded-rect, ~8–12 dp radius, translucent
  white ~6–10% fill + hairline stroke), temp big-ish + condition text +
  a simple condition glyph drawn in code (no icon library). Position: bottom
  corner or under the date — pick what balances the composition on 960x480.
- Link dot: bottom edge, small (~6–8 dp), soft outer glow matching its color,
  plus (optional) tiny "online" label in muted text only when NOT connected.

## Server + protocol work (required, keep it small)
- `server/` (Python echo-server): add a lightweight weather poller. Open-Meteo
  needs no API key: fetch
  `https://api.open-meteo.com/v1/forecast?latitude=43.0481&longitude=-76.1474&current=temperature_2m,weather_code,is_day&timezone=auto`
  every 10 minutes (one-shot on connect too; failures → keep last value, never
  crash the server loop). Location from env `WEATHER_LAT`/`WEATHER_LON` in `.env`
  (defaults = Syracuse NY above). New env keys documented in `.env.example`.
- Push to the device over the existing WS link as a new protocol envelope (read
  `server/protocol.py` + client `Protocol.kt` to match conventions): type
  `weather` with `{temp_c, code, is_day}` (code = WMO weather code, client maps
  to a small icon set: clear/partly/cloud/rain/snow/storm/fog + night variants).
  Send on link connect and every poll. Client stores latest, renders card,
  shows placeholder "–" until first value arrives.
- Run the existing server test suite; extend it with a weather-payload test.

## Hard constraints
- NO Jetpack Compose migration. Views + Canvas only (project is deliberately
  lightweight for a 974 MiB device).
- Do not touch: alarm/timer/schedule screens logic, `Link.kt` reconnect logic,
  `AudioCapture`/`AudioPlayback`/`CameraSource`, the wake-word server flow,
  `docs/` other than adding this brief (already committed), `tools/`.
- No new Gradle dependencies unless truly unavoidable (state why).
- Client stays armeabi-v7a-only, minSdk 30, builds with the existing gradle
  setup (JAVA_HOME = `C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot`,
  ANDROID_HOME = `C:\Users\jaide\Android\Sdk`).
- The ROM source build (WSL) is running on this PC — do NOT touch WSL, do NOT
  run anything in `wsl.exe`. Windows-side gradle builds are fine.
- Weather/ambient data path must NEVER block the voice link.

## Deliverable + verification
- Code changes committed with a clear message (client + server in one commit or
  two logical ones).
- `./gradlew assembleDebug` (in `EchoTerminal/`) succeeds → APK at
  `EchoTerminal/app/build/outputs/apk/debug/app-debug.apk`.
- `python -m pytest server/ -q` passes (Windows: `.venv/Scripts/python.exe`).
- Report (keep to ~15 lines): what changed per file, the palette/time stops you
  chose, how weather renders pre-first-push, commit SHA(s), anything deferred.
  Do NOT claim done unless the APK builds and the server suite passes.
