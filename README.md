# echo-gemini

Turning an **Amazon Echo Show 5 (1st gen, 2019, codename `checkers`)** into a
self-hosted Gemini appliance: an ambient display that is a clock by default, a
voice terminal you can talk to, and a camera Gemini can see through.

The full product plan — every decision, version constraint and source URL — is
[`docs/HANDOFF.md`](docs/HANDOFF.md). **Read that first.** This README is the
runbook.

---

## What it is

Three things at once:

1. **An everyday ambient display.** A clock by default, plus arbitrary content
   pushed over an HTTP API. **It keeps working when the backend is down** — that
   is a hard requirement, not a nice-to-have.
2. **A Gemini voice terminal.** Wake word, or tap anywhere, then talk.
3. **A Gemini Live *view*.** The camera streams frames so Gemini can see what
   the device sees, with an unmistakable on-screen indicator while it does.

The Show has 1 GB of RAM, a 32-bit ARM userspace and one weak microphone, so it
does none of the thinking. It captures, it renders, and it holds one socket
open.

---

## Architecture

```
┌─ Echo Show 5 (LineageOS 18.1 custom build, rooted, kiosk) ──┐
│                                                              │
│  EchoTerminal.apk  (Kotlin, minSdk 30, armeabi-v7a)          │
│    ├─ Ambient layer   LOCAL clock — zero network dependency  │
│    │                  + WebView overlay for pushed content   │
│    ├─ AudioCapture    16 kHz mono PCM16 + gain + VAD gate    │
│    ├─ AudioPlayback   24 kHz mono PCM16 + barge-in flush     │
│    ├─ CameraSource    1 FPS JPEG ≤768², single-client,       │
│    │                  opened on demand, closed cleanly       │
│    └─ Link            ONE persistent outbound WebSocket      │
│                                                              │
│  Tailscale (sideloaded APK, armeabi-v7a, no Play Services)   │
└───────────────────────────┬──────────────────────────────────┘
                            │ Tailscale (WireGuard)
                            │ ↑ audio, video   ↓ audio, display
┌───────────────────────────▼──────────────────────────────────┐
│  Windows PC — always on                                       │
│                                                               │
│  echo-server  (Python)                                        │
│    ├─ WebSocket hub    device link, heartbeat, reconnect      │
│    ├─ Wake word        openWakeWord (ONNX) over audio stream  │
│    ├─ Gemini Live      google-genai SDK → Live API            │
│    ├─ Display API      POST /display → pushed to device       │
│    └─ GEMINI_API_KEY   never leaves this machine              │
└───────────────────────────┬──────────────────────────────────┘
                            ▼  wss:// … BidiGenerateContent
                     Google Gemini Live API
```

**Why the device dials out:** one outbound WebSocket, kept alive, multiplexing
audio up, video up, audio down and display commands down. No inbound ports on
the device, no NAT problems, and reconnection logic lives in exactly one place.

**Why a proxy rather than direct-to-Google:** the API key stays on the PC, the
heavy lifting runs on real hardware, and the wake word can be retuned against
logged audio without reflashing anything. The added latency is one Tailscale hop
to a machine in the same house.

---

## ⚠️ Read this before you reflash anything

> **Reinstalling the ROM silently breaks the camera until the shim step is
> re-run.**
>
> This is the most common post-install failure on these devices (HANDOFF §5.9,
> §12). The only symptom is `Number of camera devices: 0` — everything else
> looks completely normal. After *any* ROM reinstall, re-run HANDOFF §5.7 step 3
> (`shims/libcmdqevent/build.sh` then `scripts/install-cmdq-event-shim.sh`,
> then reboot) and record it in the table in
> [`docs/HARDWARE-STATUS.md`](docs/HARDWARE-STATUS.md) §5.

Two more, while you are here:

- **Do not connect the device to Wi-Fi before Phase 1 completes.** An OTA can
  patch the LK bootloader and permanently close the exploit window. The offline
  state is what is protecting it.
- **Use amonet 2.0.1 or newer**, branch `mt8163-checkers`. The camera patches
  cannot run on a device unlocked with 1.x, and the partition layout differs —
  getting it wrong means redoing Phase 1 entirely.

---

## Repo layout

```
echo-gemini/
├── README.md                      # you are here
├── .env.example                   # every server config var; copy to .env
├── pytest.ini
├── docs/
│   ├── HANDOFF.md                 # the product plan — source of truth
│   ├── FLASHING.md                # Phase 1 expanded
│   ├── ROM-BUILD.md               # Phase 2 expanded
│   └── HARDWARE-STATUS.md         # capability matrix + MEASURED mic and RAM
├── server/
│   ├── main.py                    # WebSocket hub + HTTP display API
│   ├── protocol.py                # wire schema (mirrored by the client)
│   ├── config.py                  # environment-only configuration
│   ├── wake.py                    # openWakeWord over the inbound audio
│   ├── gemini_live.py             # Live session lifecycle, audio + video
│   ├── requirements.txt
│   ├── install-service.ps1        # Task Scheduler at-boot registration
│   ├── tests/                     # pytest: no hardware, no key, no network
│   └── tools/
│       ├── fake_device.py         # pretends to be the Show
│       ├── test_display.py        # live-fire display API demo
│       └── assets/                # 16 kHz TTS sample, 768² test JPEG
└── EchoTerminal/
    ├── local.properties.example   # server URL + shared secret
    └── app/src/main/java/com/echogemini/terminal/
        ├── MainActivity.kt        # launcher, layer switching
        ├── AmbientClock.kt        # local clock — NO network dependency
        ├── PushSurface.kt         # WebView overlay for pushed content
        ├── StatusOverlay.kt       # UI state + CAMERA ON indicator
        ├── Link.kt                # persistent WebSocket, backoff, heartbeat
        ├── Protocol.kt            # mirror of server/protocol.py
        ├── AudioCapture.kt        # AudioRecord + gain + VAD gate
        ├── AudioPlayback.kt       # AudioTrack + barge-in flush
        ├── CameraSource.kt        # single-owner, ≤1 FPS, safe lifecycle
        └── BootReceiver.kt
```

---

## Phases

| Phase | What | Where | Owner |
|---|---|---|---|
| 1 | Unlock and flash stock LineageOS 18.1 | HANDOFF §4, `docs/FLASHING.md` | hardware |
| 2 | Build LineageOS with camera + Bluetooth patches | HANDOFF §5, `docs/ROM-BUILD.md` | hardware |
| 3 | Device baseline: Tailscale, **measure the mic** | HANDOFF §6 | hardware |

> **Tailscale auth on the Show (decision):** the device never logs into Google.
> Default = **auth key** from `tailscale.com/admin/settings/keys`, wired headlessly with
> root via `tailscaled up --authkey=…` (userspace-networking mode) — no browser needed.
> Fallback = Tailscale app shows a login URL+code; open it on the laptop and complete
> Google SSO there.
| 4 | **Server** (`server/`) | HANDOFF §7 | ← this repo's code |
| 5 | **Client** (`EchoTerminal/`) | HANDOFF §8 | ← this repo's code |

Phases 4 and 5 are built and can be exercised **before** the hardware exists —
that is what `server/tools/fake_device.py` is for, and it is HANDOFF
verification step 11.

---

## Server quickstart

Nothing below needs the Show, and nothing below needs a Gemini API key. With no
key the server starts, holds the device link, runs the wake word and serves
`POST /display` — it just logs that the Gemini leg is disabled.

### 1. Install

Python 3.11+ on Windows. Every dependency installs from a prebuilt wheel; no
compiler needed.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r server\requirements.txt
```

### 2. Configure

```powershell
Copy-Item .env.example .env
```

Then edit `.env`. The only *required* value is the shared secret — the server
refuses to start without one, because an unauthenticated WebSocket on the
tailnet is exactly what it guards against:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste that into `ECHO_SHARED_SECRET`. Leave `GEMINI_API_KEY` empty for now.

`.env` is gitignored. **The API key lives only on this machine — never in the
repo, never on the device.**

### 3. Run

```powershell
python server\main.py
```

```
INFO  echo.server  echo-server starting: listen=0.0.0.0:8765 live=off (no GEMINI_API_KEY) wake=alexa vision=on_demand idle_close=120s
```

The first run downloads the openWakeWord ONNX models (~8 MB); startup is slower
that once.

### 4. Prove it works, with no hardware

Three terminals.

```powershell
# 1 — the server
python server\main.py

# 2 — a fake Echo Show: connects, streams a bundled TTS sample, prints
#     everything the server sends
python server\tools\fake_device.py
```

```
[   0.00s] <- welcome  protocol=v1 heartbeat=15s audio=16000->24000 Hz live=DISABLED (no API key)
[   0.09s] <- mic      OPEN gain=1
[   0.41s] -> streaming wake-sample.wav: 5.0s x1 as 20 ms frames
[   1.12s] <- state    LISTENING          <-- the wake word fired
[   4.11s] <- ping 1   -> pong
```

```powershell
# 3 — push each display type at it
python server\tools\test_display.py
```

Terminal 2 shows every push arriving on the device socket. Other useful flags:

```powershell
python server\tools\fake_device.py --no-audio          # just hold the link
python server\tools\fake_device.py --tap               # tap-to-talk
python server\tools\fake_device.py --stop-tap 1        # tap-to-stop, once
python server\tools\fake_device.py --shutter           # simulate the privacy latch
python server\tools\fake_device.py --save-reply out.wav
```

### 5. Tests

```powershell
python -m pytest server\
```

No hardware, no API key, no network. Covers protocol round-trips, display API
validation and routing with a device attached, wake-word gating, and session
idle-close against a mocked Live client.

### 6. Turn Gemini on

Get a key from <https://aistudio.google.com/apikey>, put it in `.env` as
`GEMINI_API_KEY`, and restart.

**Confirm the model id before you do.** The Live model line has been renamed
repeatedly — `GEMINI_MODEL` is configuration, not a constant. Check
<https://ai.google.dev/api/live> for the current id and set it in `.env`. The
shipped default is `gemini-2.0-flash-live-001`.

Then:

```powershell
python server\tools\fake_device.py --tap --save-reply reply.wav
```

Speak-to-it-and-hear-it-back, entirely on the desktop. This proves key, quota
and model id while debugging is still cheap (HANDOFF verification step 11).

### 7. HTTP API

| Route | Auth | Does |
|---|---|---|
| `GET /health` | none | status: link, session, wake word, vision |
| `GET /ws` | `X-Echo-Secret` | the device link |
| `POST /display` | `X-Shared-Secret` | push content; **503** if no device |
| `POST /display/clear` | `X-Shared-Secret` | back to the clock |
| `POST /vision` | `X-Shared-Secret` | manual camera override |

```bash
curl -X POST http://your-pc:8765/display \
  -H "X-Shared-Secret: $ECHO_SHARED_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"type":"text","payload":{"text":"Bus 62 in 4 min","subtitle":"Westcott"},"duration":30}'
```

`type` is one of `text | html | image | timer`. `duration` is seconds (`0` =
until replaced). `priority` breaks ties. `html` is why "anything via API" is
true — weather, calendar, transit, dashboards, with no new APK.

---

## Running the server as a service

```powershell
# from an ELEVATED PowerShell, at the repo root
.\server\install-service.ps1
```

Registers an at-boot Task Scheduler task running as SYSTEM with auto-restart. It
writes **no secrets** into the task definition — those stay in `.env`.

> **Deployment note (Jaiden's PC):** port **8765 is taken by the Orbit prod server**
> (autostarts at boot, elevated). The local `.env` sets `ECHO_PORT=8766` — keep it
> that way. The examples below show 8765 as the code default; on this machine use 8766.

```powershell
.\server\install-service.ps1 -AddFirewallRule -Port 8766   # optional; 8766 on this PC
.\server\install-service.ps1 -Uninstall
Get-Content server\data\service.log -Tail 50 -Wait
```

### Manual steps the script deliberately does not do

An always-on backend that goes to sleep is not one. The script does not rewrite
your machine's power plan; do it yourself:

1. **Disable sleep and hibernate**

   ```powershell
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   powercfg /hibernate off
   ```

2. **Disable USB selective suspend**

   ```powershell
   powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
   powercfg /setactive SCHEME_CURRENT
   ```

3. **Disable fast startup**, which can leave the network stack half-initialised
   after a power cut: Control Panel → Power Options → *Choose what the power
   buttons do* → uncheck *Turn on fast startup*.

4. **Scope the firewall to Tailscale.** The `-AddFirewallRule` switch opens the
   whole Private profile. Narrow it, or better, set `ECHO_HOST` in `.env` to the
   machine's Tailscale IP so the listener never binds anywhere else.

---

## Client build

The debug APK builds with JDK 17 and the Android SDK. The v2 implementation also
has JVM tests for local scheduling, persistence, snooze, and calendar arithmetic.
See [v2 verification](docs/BUILD-3-RESULTS.md) for commands and results.
Hardware deployment and on-device testing are separate gates.

### What you need

| | |
|---|---|
| JDK | 17 (AGP 8.1 requires it) |
| Android SDK | Platform 34 (compile), build-tools 34 |
| Gradle | 8.4 — via the wrapper |
| Android Studio | Hedgehog or later, if you want an IDE |

### Gradle wrapper

The wrapper JAR, properties, and scripts are committed. Use `gradlew.bat` on
Windows or `./gradlew` on Unix; a global Gradle installation is unnecessary.

### Configure and build

```powershell
Copy-Item EchoTerminal\local.properties.example EchoTerminal\local.properties
```

Set `echo.serverUrl` to the PC's Tailscale MagicDNS name and `echo.sharedSecret`
to the **same value** as `ECHO_SHARED_SECRET` in `.env`. Then:

```powershell
cd EchoTerminal
.\gradlew.bat testDebugUnitTest assembleDebug
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

> The shared secret is compiled into `BuildConfig` and is recoverable from the
> APK by anyone holding the device. That is acceptable: it is defence in depth
> behind the tailnet, not the security boundary. It is also exactly why the
> **`GEMINI_API_KEY` never goes near the device** at all.

### Make it the launcher

```powershell
adb shell pm grant com.echogemini.terminal android.permission.RECORD_AUDIO
adb shell pm grant com.echogemini.terminal android.permission.CAMERA
adb shell cmd package set-home-activity com.echogemini.terminal/.MainActivity
adb reboot
```

### Test the requirement that matters

Deploy the app **with the server stopped**. The clock must render, survive a
reboot, and show a clear disconnected state. This is HANDOFF verification step
12 — *test it by making it fail*.

---

## Configuration reference

Every variable is documented in [`.env.example`](.env.example). The ones worth
knowing about:

| Variable | Default | Why you would change it |
|---|---|---|
| `ECHO_SHARED_SECRET` | *(none — required)* | Must match the client |
| `GEMINI_API_KEY` | *(empty)* | Empty = Live off, everything else on |
| `GEMINI_MODEL` | `gemini-2.0-flash-live-001` | **Check <https://ai.google.dev/api/live>** |
| `SESSION_IDLE_TIMEOUT` | `120` | Live bills by session duration |
| `WAKE_MODEL` | `alexa` | `hey_jarvis`, `hey_mycroft`, or a custom `.onnx` |
| `WAKE_THRESHOLD` | `0.5` | Raise if the TV sets it off; lower if it misses you |
| `VISION_MODE` | `on_demand` | `off`, or `always` |
| `MIC_GAIN` | `1.0` | **A placeholder.** Set it from the §1.1 measurement |
| `RECORD_AUDIO_DIR` | *(empty)* | Log raw mic audio for wake-word tuning |

---

## Design decisions you should not undo

Each of these is in the code for a reason that cost someone something to learn.

- **The clock has no network dependency.** `AmbientClock` does not import
  `Link`. If the PC is off, the Show is still a clock. Do not "improve" this by
  making the clock wait for anything.
- **Never force-kill `cameraserver`.** Doing so while streaming causes an IOMMU
  livelock that requires physically power-cycling the device. `CameraSource` has
  no `killProcess`, no force-stop and no retry loop, and must never gain one. On
  error it releases, reports, and latches off.
- **One camera owner, opened on demand.** HAL1 is single-client. The camera opens
  only when the server asks for vision and is released the moment the session
  ends, on every exit path.
- **The camera indicator sits above pushed content.** A camera in a home
  streaming to a cloud model must be visibly doing so.
- **Idle-close is cost control, not polish.** Gemini Live bills for the whole
  time a session is open.
- **Tap-to-talk is permanent.** The mic is weak enough that the wake word will
  sometimes miss.
- **Tap during a conversation ends it.** One gesture, and which of the two it
  means is whatever is already on screen. Mid-answer taps cut the answer off,
  which is the point: a hand going to the panel means *enough*.
- **Half-duplex on purpose.** Suppressing the uplink while the server speaks
  sidesteps acoustic echo cancellation on a device with one weak microphone.
- **Voice and vision processing stay on the server.** The clock, alarms and
  timers run locally so they keep working with the PC switched off.

---

## Known limitations

- **The microphone is the real risk.** One mic of the array, low gain. Whether
  far-field wake word is viable is empirical and only answerable on the unit —
  see `docs/HARDWARE-STATUS.md` §1.1.
- **The Windows PC is a single point of failure for voice and vision** —
  deliberately not for the clock.
- **You lose Alexa permanently.** Only the TWRP backup restores it.
- **Camera frames go out at 720×720, not 768×768.** The sensor's native capture
  is 1280×720 and its centre square is 720; the API recommends 768 rather than
  requiring it, and upscaling would invent detail.
- **Hardware validation of the v2 alarm build remains outstanding.** See the
  [verification report](docs/BUILD-3-RESULTS.md).

---

## Reference

- Live API — <https://ai.google.dev/api/live>
- Live API capabilities (audio + video) — <https://ai.google.dev/gemini-api/docs/live-api/capabilities>
- openWakeWord — <https://github.com/dscripka/openWakeWord>
- amonet, `mt8163-checkers` branch — <https://github.com/R0rt1z2/amonet/tree/mt8163-checkers>
- Camera and Bluetooth patches — <https://github.com/jxlarrea/lineageos-echo-show-camera>
- Tailscale APKs (`armeabi-v7a`, no Play Services) — <https://pkgs.tailscale.com>

Everything else, including the full flashing and ROM-build sequences, is in
[`docs/HANDOFF.md`](docs/HANDOFF.md).
