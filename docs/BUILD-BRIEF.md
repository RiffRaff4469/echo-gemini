# Echo-Gemini — Software Build Brief (Track A)

You are the autonomous implementer in this repo (`C:\Users\jaide\projects\echo-gemini`).
**Source of truth: read `docs/HANDOFF.md` IN FULL before writing anything.** It is the
approved product plan for turning an Amazon Echo Show 5 (gen 1, "checkers") into a
thin-terminal Gemini appliance. Where this brief and the HANDOFF disagree, the HANDOFF wins
except where this brief explicitly narrows scope.

You build the software slices below. The hardware phases (flashing, ROM build) are owned
elsewhere and are NOT your job. Do not write files under `docs/FLASHING.md` or
`docs/ROM-BUILD.md` — the orchestrator owns those paths. Do not delete or edit
`docs/HANDOFF.md`.

## Architecture in one paragraph (from HANDOFF §3)

The device runs ONE persistent outbound WebSocket to a Python server on a Windows PC over
Tailscale. The device streams gated 16 kHz mono PCM16 audio up; the server runs wake-word
detection, opens Gemini Live sessions via the `google-genai` Python SDK, streams audio back
at 24 kHz, and — when vision is wanted — requests ≤1 FPS 768×768 JPEG frames from the
device which it forwards as `realtimeInput.video`. A `POST /display` HTTP API pushes display
content down the same socket. The API key lives ONLY on the server, never on the device,
never in git. When the server is down the device must still render a local clock.

## Deliverables (all code, in order)

### 1. `server/` — Python echo-server (HANDOFF §7, repo layout §10)

- `main.py` — WebSocket hub + HTTP display API. One `asyncio` process. Accepts the device's
  single persistent connection; heartbeat (~15 s ping/pong); exponential-backoff reconnect
  is the CLIENT's job but the server must tolerate flapping cleanly (no half-open sockets).
  Multiplexes channels over one socket (see Protocol below).
- `protocol.py` — shared message schema, versioned envelope. Both server and a future Kotlin
  client mirror this; keep it small, typed (dataclasses), with encode/decode unit tests.
- `wake.py` — openWakeWord (ONNX Runtime) over the inbound 16 kHz stream. Config-driven
  model name; default `alexa` unless a better far-field default exists in the lib. Frame the
  VAD-gated stream; on trigger, signal the hub.
- `gemini_live.py` — Live API session lifecycle with the `google-genai` SDK:
  - Audio IN: device 16 kHz PCM16 → API (pass through, no resample).
  - Audio OUT: API 24 kHz PCM16 → device (pass through).
  - Video IN: server-requested frames (≤1 FPS) → `realtimeInput.video`,
    `mimeType: "image/jpeg"`. Session `media_resolution` from config.
  - **Idle-close:** close the Live session after `SESSION_IDLE_TIMEOUT` (default 120 s) of
    no speech — sessions bill by duration. Reopen on next wake.
  - Barge-in: an interrupt message from the API must flush device playback (send an
    `interrupt` control message down the socket).
  - Model id is CONFIG, not a constant: `GEMINI_MODEL` env with a sensible current default.
    Note in README that the Live model line has been renamed repeatedly and where to check
    (https://ai.google.dev/api/live).
- HTTP display API: `POST /display` with JSON `{type, payload, duration, priority}`,
  `type` ∈ `text | html | image | timer` (HANDOFF §8.1). Validates, then pushes a `display`
  message down the device socket. Returns 503 if no device is connected. Optional
  `X-Shared-Secret` header check (config).
- WebSocket handshake requires a shared-secret header (config; defense in depth over the
  tailnet — HANDOFF §7).
- `install-service.ps1` — registers the server as a Windows at-boot service via Task
  Scheduler (primary; NSSM acceptable alternative). Also: disable sleep/hibernate and USB
  selective suspend are OUT OF SCOPE for the script — put them in README as manual steps.
  `GEMINI_API_KEY` must be read from the environment / a gitignored `.env`, never embedded.
- `requirements.txt` — pinned-ish deps: `google-genai`, `websockets` (or `aiohttp` if you
  prefer one lib for both WS server + HTTP), `openwakeword`, `onnxruntime`, `python-dotenv`,
  `pytest`, `httpx` (tests). Keep the list minimal and Windows-installable (pure wheels).
- `.env.example` (committed) listing every config var with a placeholder; real `.env`
  gitignored.
- `tools/fake_device.py` — a script that pretends to be the Show: connects over WS with the
  shared secret, streams a bundled WAV of speech (record one with a TTS or ship a tiny
  sample), prints everything the server sends, and can emit a JPEG frame on demand. This is
  the no-hardware test harness (HANDOFF verification #11).
- `tools/test_display.py` — curl-able demo / pytest that POSTs each display type and asserts
  the device socket receives it.

### 2. `EchoTerminal/` — Kotlin Android client (HANDOFF §8, repo layout §10)

Full Gradle scaffold (`settings.gradle.kts`, root + `app` build files, wrapper properties;
version catalog optional). Constraints: **minSdk 30, targetSdk 33, ABI `armeabi-v7a` only,
Kotlin, zero Google Play dependencies, zero GApps assumptions.** The APK itself will be
compiled later on a machine with the Android SDK — you produce complete, reviewable source,
NOT a built APK, and you will NOT claim the APK builds.

- `MainActivity.kt` — device launcher (`android.intent.action.MAIN` +
  `CATEGORY_HOME`/`CATEGORY_LAUNCHER` registration so it can be set as the kiosk home),
  layer switching between ambient clock and push surface, `FLAG_KEEP_SCREEN_ON`,
  `RECEIVE_BOOT_COMPLETED` handling, UI states idle/listening/thinking/speaking with
  room-legible styling for a 960×480 panel.
- `AmbientClock.kt` — native locally-rendered clock (time, date, subtle link indicator).
  **ZERO network dependency.** This is the explicit user requirement: device stays a clock
  when the server is off.
- `PushSurface.kt` — WebView overlay above the clock for pushed content; renders on
  `display` command, fades back to the clock on expiry (`duration`) or link loss.
- `Link.kt` — one persistent outbound WebSocket; shared-secret header; heartbeat; reconnect
  with exponential backoff; message dispatch. Link state NEVER blocks the clock.
- `AudioCapture.kt` — `AudioRecord`, 16 kHz mono PCM16, ~20 ms frames, configurable software
  gain, then a simple on-device VAD gate so silence is not streamed. Suppress uplink while
  server is speaking (half-duplex; HANDOFF §8.2).
- `AudioPlayback.kt` — `AudioTrack` 24 kHz mono PCM16; flush immediately on `interrupt`
  (barge-in).
- `CameraSource.kt` — single-owner camera object. HAL1 constraints from HANDOFF §2 encoded
  as code comments AND logic: open on demand, release cleanly, one owner, never
  `killProcess`/force-stop `cameraserver`, no aggressive restart on error (back off and
  surface). Capture ≤1 FPS, downscale 1280×720 → 768×768, JPEG, send as video frame. Detect
  near-black frames → send a `shutter-closed` notice instead of silently streaming black.
  Server requests vision → camera opens; session ends → camera released.
- Display command model mirrors `server/protocol.py`.
- No camera app open / no capture until vision is requested; on-screen indicator whenever
  frames are being sent (HANDOFF §8.3 — a camera streaming to a cloud model must be visible).

### 3. README.md + `docs/HARDWARE-STATUS.md` template

README: what this is, architecture diagram (ASCII, mirror HANDOFF §3), repo layout,
server quickstart (venv, `.env` from `.env.example`, run, fake-device test), client build
steps (SDK requirements — deferred), the Phase 1/2/3 pointers, prominent note: **"ROM
reinstall silently breaks the camera until the shim step is re-run"**, and the manual
Windows power settings list (sleep/hibernate/USB selective suspend). HARDWARE-STATUS.md: the
capability matrix from HANDOFF §9 as a template with blank MEASURED cells (mic gain, real
RAM) to be filled during hardware bring-up.

## Hard constraints

1. **No secrets in git, ever.** `GEMINI_API_KEY` and the shared secret come from env/.env
   only. If you find yourself about to write a real key anywhere, stop. `.env.example`
   placeholders only.
2. No Play Services, no GApps, no proprietary deps in the client. Server deps must be
   Windows-installable pure wheels (no compilation).
3. Follow the HANDOFF's device-lifecycle rules (§8.3): no force-killing `cameraserver`, no
   camera retry loops, single-client enforcement. These are product requirements, not style.
4. Do not create/edit `docs/FLASHING.md` or `docs/ROM-BUILD.md` (orchestrator-owned).
5. Do not edit `docs/HANDOFF.md`.
6. No "temporary" code paths that violate the thin-terminal architecture (all logic on
   server, device dumb).
7. Real audio/session code only — no stubbed "TODO: implement" functions in server paths
   that the fake-device harness exercises. If something is genuinely deferred, it must be a
   documented no-op that logs, not a silent stub.

## Testing / done definition (server)

- `python -m pytest server/` passes: protocol encode/decode round-trips, display API
  validation + routing (fake device connected), VAD/wake gating unit logic (mock frames),
  session idle-close logic (mocked Live client).
- `python tools/fake_device.py` against a running server (no API key needed for the
  socket/display path — the Live leg only activates on wake) demonstrates: connect with
  secret, heartbeat, POST /display → message arrives on the socket, clean shutdown.
- Live API leg is config-gated: with no `GEMINI_API_KEY` the server must start and serve
  display/socket paths, logging that Live is disabled.
- Client: Kotlin sources compile-clean in intent — no syntax errors visible on review, no
  missing imports of nonexistent classes. (Real compile happens later; say so honestly.)
- `git status --short` clean at the end; commit in logical slices as you go
  (`server: protocol + ws hub`, `server: wake word`, `server: gemini live`, `server: display
  api + service script`, `client: scaffold`, `client: ambient + link`, `client: audio`,
  `client: camera`, `docs: README + status template`). Never commit a broken intermediate.

## Final stdout report contract

End your run by printing EXACTLY these sections (last thing on stdout):

1. **Gates** — for each deliverable: what you ran and the evidence line (test counts,
   fake-device transcript snippet, file list). Do not claim done without the evidence.
2. **Commits** — `git log --oneline` of your work.
3. **What is NOT done** — anything deferred or knowingly incomplete (client compile, Live
   leg without a key, etc.).
4. **Risks / open items** — things the orchestrator must verify on hardware or with a real
   key.

Then STOP. No closing pleasantries, no extra summary.
