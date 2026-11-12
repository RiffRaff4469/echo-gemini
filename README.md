# echo-gemini

Turning an **Amazon Echo Show 5 (1st gen, 2019, codename `checkers`)** into a
self-hosted, Google-Gemini-powered assistant: a clock that never needs the
network, a voice terminal you talk to, a camera Gemini can see through, and a
Spotify speaker.

> **Status: work in progress.** The software side is built and tested; the
> hardware side is done on one unit and not generalised. Nothing here is a
> polished, installable product — expect to read code, and expect the Gemini
> model id and librespot flags to need checking against whatever is current when
> you try it. See [Known limitations](#known-limitations).

---

## What it does

1. **An ambient display.** A clock by default, plus arbitrary content pushed
   over an HTTP API. It keeps working when the backend is down — that is a hard
   requirement, not a nice-to-have.
2. **A Gemini voice terminal.** Wake word, or tap anywhere, then talk.
3. **A Gemini Live view.** The camera streams frames so Gemini can see what the
   device sees, with an unmistakable on-screen indicator while it does.
4. **A Spotify speaker.** librespot runs on the host and streams PCM down the
   same socket, so "play some lofi" works by voice and the phone sees a Connect
   device called *Jarvis*.

The Echo Show 5 has 1 GB of RAM, a 32-bit ARM userspace and one weak
microphone, so it does none of the thinking. It captures, it renders, and it
holds one socket open.

---

## Architecture

The device runs a custom LineageOS 18.1 build with a kiosk app in Kotlin. A
small Python server on any always-on machine does the audio, video and inference
work. The two are joined by a single WebSocket over a private network
(Tailscale in this build, but any WireGuard/WSS path works).

```
┌─ Echo Show 5 (custom LineageOS 18.1, rooted, kiosk) ────────┐
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
                            │ private network (WireGuard)
                            │ ↑ audio, video   ↓ audio, display
┌───────────────────────────▼──────────────────────────────────┐
│  Host machine — any always-on box (x86 Linux, Windows, a     │
│  small server; this build was developed on Windows)          │
│                                                              │
│  echo-server  (Python)                                       │
│    ├─ WebSocket hub    device link, heartbeat, reconnect     │
│    ├─ Wake word        openWakeWord (ONNX) over audio stream │
│    ├─ Gemini Live      google-genai SDK → Live API           │
│    ├─ Display API      POST /display → pushed to device      │
│    └─ GEMINI_API_KEY   never leaves this machine             │
└───────────────────────────┬──────────────────────────────────┘
                            ▼  wss:// … BidiGenerateContent
                     Google Gemini Live API
```

**Why the device dials out.** One outbound WebSocket, kept alive, multiplexing
audio up, video up, audio down and display commands down. No inbound ports on
the device, no NAT problems, and reconnection logic lives in exactly one place.

**Why a proxy rather than direct-to-Google.** The API key stays on the host, the
heavy lifting runs on real hardware, and the wake word can be retuned against
logged audio without reflashing anything. The cost is one extra network hop on
the local network.

---

## Repo layout

```
echo-gemini/
├── README.md                      # you are here
├── .env.example                   # every server config var; copy to .env
├── pytest.ini
├── prompts/
│   └── jarvis-system.txt          # the system instruction used for the demo unit
├── server/
│   ├── main.py                    # WebSocket hub + HTTP display API
│   ├── protocol.py                # wire schema (mirrored by the client)
│   ├── config.py                  # environment-only configuration
│   ├── wake.py                    # openWakeWord over the inbound audio
│   ├── gemini_live.py             # Live session lifecycle, audio + video
│   ├── alarms.py                  # alarm/timer scheduling + acknowledgement
│   ├── weather.py                 # Open-Meteo polling for the ambient card
│   ├── govee.py                   # optional BLE light control
│   ├── memory.py                  # small local memory store
│   ├── spotify.py                 # librespot supervisor, arbitration, cards
│   ├── spotify_api.py             # the Spotify Web API calls, and only those
│   ├── spotify_auth.py            # OAuth (PKCE) + the gitignored token file
│   ├── resample.py                # 44.1 kHz stereo → 24 kHz mono PCM16
│   ├── requirements.txt
│   ├── install-service.ps1        # Windows Task Scheduler registration
│   ├── tests/                     # pytest: no hardware, no key, no network
│   └── tools/                     # fake device, display demo, spotify login
└── EchoTerminal/
    ├── local.properties.example   # server URL + shared secret
    └── app/src/main/java/com/echogemini/terminal/
        ├─ MainActivity.kt         # launcher, layer switching
        ├─ AmbientClock.kt         # local clock — no network dependency
        ├─ AmbientWeb.kt           # WebView layer for pushed content
        ├─ PushSurface.kt          # overlay surface
        ├─ StatusOverlay.kt        # UI state + CAMERA ON indicator
        ├─ ScheduleScreen.kt       # alarms and timers, rendered locally
        ├─ AlarmScheduler.kt       # local scheduling, survives reboot
        ├─ Stopwatch.kt
        ├─ Link.kt                 # persistent WebSocket, backoff, heartbeat
        ├─ Protocol.kt             # mirror of server/protocol.py
        ├─ AudioCapture.kt         # AudioRecord + gain + VAD gate
        ├─ AudioPlayback.kt        # AudioTrack + barge-in flush (the voice)
        ├─ MusicPlayback.kt        # a SECOND AudioTrack, USAGE_MEDIA (Spotify)
        ├─ CameraSource.kt         # single-owner, ≤1 FPS, safe lifecycle
        └─ BootReceiver.kt
```

---

## Server quickstart

Nothing below needs the Show, and nothing below needs a Gemini API key. With no
key the server starts, holds the device link, runs the wake word and serves
`POST /display` — it just logs that the Gemini leg is disabled. That is the
supported way to bring the software up before the hardware exists.

### 1. Install

Python 3.11+. Every dependency installs from a prebuilt wheel; no compiler
needed. The commands below are PowerShell — on Linux/macOS substitute
`python3 -m venv .venv && . .venv/bin/activate`.

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
refuses to start without one, because an unauthenticated WebSocket on a private
network is exactly what it guards against:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste that into `ECHO_SHARED_SECRET`. Leave `GEMINI_API_KEY` empty for now.

`.env` is gitignored. **The API key lives only on the host — never in the repo,
never on the device.**

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
python -m pytest
```

No hardware, no API key, no network. Covers protocol round-trips, display API
validation and routing with a device attached, wake-word gating, alarm
scheduling, and session idle-close against a mocked Live client.

### 6. Turn Gemini on

Get a key from <https://aistudio.google.com/apikey>, put it in `.env` as
`GEMINI_API_KEY`, and restart.

**Confirm the model id before you do.** The Live model line gets renamed
regularly — `GEMINI_MODEL` is configuration, not a constant. Check
<https://ai.google.dev/api/live> for the current id and set it in `.env`.

Then:

```powershell
python server\tools\fake_device.py --tap --save-reply reply.wav
```

Speak to it and hear it back, entirely on the desktop. This proves key, quota
and model id while debugging is still cheap.

### 7. HTTP API

| Route | Auth | Does |
|---|---|---|
| `GET /health` | none | status: link, session, wake word, vision |
| `GET /ws` | `X-Echo-Secret` | the device link |
| `POST /display` | `X-Shared-Secret` | push content; **503** if no device |
| `POST /display/clear` | `X-Shared-Secret` | back to the clock |
| `POST /vision` | `X-Shared-Secret` | manual camera override |
| `GET /spotify/art/{id}` | none | album art proxy for the device |

```bash
curl -X POST http://your-host:8765/display \
  -H "X-Shared-Secret: $ECHO_SHARED_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"type":"text","payload":{"text":"Bus 62 in 4 min","subtitle":"Westcott"},"duration":30}'
```

`type` is one of `text | html | image | timer | panel`. `duration` is seconds
(`0` = until replaced). `priority` breaks ties. `html` is why "anything via API"
is true — weather, calendar, transit, dashboards, with no new APK.

---

## Spotify

Optional and off by default. librespot runs on the host as a subprocess,
registers as a Spotify Connect speaker, and its PCM is resampled
(44.1 kHz stereo → 24 kHz mono) and sent down the existing device link on its
own audio channel — so the Show stays a speaker and a display, with no Spotify
client, no DRM and no Google services on the device.

Needs Premium, a librespot binary on the host, and a one-time browser login
(`server/tools/spotify_login.py`). All the variables are documented in
`.env.example`.

**Unproven end to end.** Everything below the binary is covered by tests against
fakes, but upstream renames librespot flags between minor releases — confirm
`--backend pipe` writes PCM to stdout before expecting audio.

---

## Client build

The debug APK builds with JDK 17 and the Android SDK. The Kotlin code also has
JVM unit tests for local scheduling, persistence and time arithmetic.

| | |
|---|---|
| JDK | 17 (AGP 8.1 requires it) |
| Android SDK | Platform 34 (compile), build-tools 34 |
| Gradle | 8.4 — via the wrapper, committed |
| Android Studio | Hedgehog or later, if you want an IDE |

```powershell
Copy-Item EchoTerminal\local.properties.example EchoTerminal\local.properties
```

Set `echo.serverUrl` to the host's reachable address (a MagicDNS name survives
the host changing IP) and `echo.sharedSecret` to the **same value** as
`ECHO_SHARED_SECRET` in `.env`. Then:

```powershell
cd EchoTerminal
.\gradlew.bat testDebugUnitTest assembleDebug
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

> The shared secret is compiled into `BuildConfig` and is recoverable from the
> APK by anyone holding the device. That is acceptable: it is defence in depth
> behind the private network, not the security boundary. It is also exactly why
> the **`GEMINI_API_KEY` never goes near the device**.

### Make it the launcher

```powershell
adb shell pm grant com.echogemini.terminal android.permission.RECORD_AUDIO
adb shell pm grant com.echogemini.terminal android.permission.CAMERA
adb shell cmd package set-home-activity com.echogemini.terminal/.MainActivity
adb reboot
```

### Test the requirement that matters

Deploy the app **with the server stopped**. The clock must render, survive a
reboot, and show a clear disconnected state. Test it by making it fail.

---

## Running the server as a service

`server/install-service.ps1` registers an at-boot Windows Task Scheduler task
running as SYSTEM with auto-restart. It writes **no secrets** into the task
definition — those stay in `.env`. On Linux/macOS use systemd or launchd
instead; the script is Windows-only.

```powershell
# from an ELEVATED PowerShell, at the repo root
.\server\install-service.ps1
.\server\install-service.ps1 -AddFirewallRule -Port 8765   # optional
.\server\install-service.ps1 -Uninstall
Get-Content server\data\service.log -Tail 50 -Wait
```

An always-on host that goes to sleep is not always-on. The script deliberately
does not touch your machine's power plan — do it yourself: disable sleep and
hibernate, disable USB selective suspend, disable fast startup, and scope the
firewall rule to your private network (better: set `ECHO_HOST` to a private-IP
address so the listener never binds anywhere else).

---

## Configuration reference

Every variable is documented in [`.env.example`](.env.example). The ones worth
knowing about:

| Variable | Default | Why you would change it |
|---|---|---|
| `ECHO_SHARED_SECRET` | *(none — required)* | Must match the client |
| `GEMINI_API_KEY` | *(empty)* | Empty = Live off, everything else on |
| `GEMINI_MODEL` | see `.env.example` | **Check <https://ai.google.dev/api/live>** |
| `SESSION_IDLE_TIMEOUT` | `120` | Live bills by session duration |
| `POST_ANSWER_SILENCE_S` | `8` | Returns to the clock after the model stops talking |
| `WAKE_MODEL` | `alexa` | `hey_jarvis`, `hey_mycroft`, or a custom `.onnx` |
| `WAKE_THRESHOLD` | `0.5` | Raise if the TV sets it off; lower if it misses you |
| `VISION_MODE` | `on_demand` | `off`, or `always` |
| `MIC_GAIN` | `1.0` | A placeholder — tune it from recordings |
| `SPOTIFY_ENABLED` | `false` | Music. Needs Premium + a one-time login |

---

## Hardware notes

The device side is the part that will cost you a weekend. Three things that
break silently:

- **Reinstalling the ROM breaks the camera** until the `libcmdqevent` shim is
  built and installed again, then rebooted. The only symptom is
  `Number of camera devices: 0` — everything else looks normal. The patch source
  this build used is <https://github.com/jxlarrea/lineageos-echo-show-camera>.
- **Do not connect the device to Wi-Fi before the unlock phase completes.** An
  OTA can patch the LK bootloader and permanently close the exploit window. The
  offline state is what is protecting it.
- **Use amonet 2.0.1 or newer**, branch `mt8163-checkers`
  (<https://github.com/R0rt1z2/amonet/tree/mt8163-checkers>). The camera patches
  cannot run on a device unlocked with 1.x, and the partition layout differs.

The unlock and LineageOS build are upstream projects; this repo starts from a
device that already boots a rooted LineageOS 18.1 with the camera and Bluetooth
patches applied.

---

## Design decisions worth not undoing

Each of these is in the code for a reason that cost something to learn.

- **The clock has no network dependency.** `AmbientClock` does not import
  `Link`. If the host is off, the Show is still a clock.
- **Never force-kill `cameraserver`.** Doing so while streaming causes an IOMMU
  livelock that requires physically power-cycling the device. `CameraSource` has
  no `killProcess`, no force-stop and no retry loop. On error it releases,
  reports, and latches off.
- **One camera owner, opened on demand.** HAL1 is single-client. The camera opens
  only when the server asks for vision and is released the moment the session
  ends, on every exit path.
- **The camera indicator sits above pushed content.** A camera streaming to a
  cloud model must be visibly doing so.
- **Idle-close is cost control, not polish.** Gemini Live bills for the whole
  time a session is open.
- **Tap-to-talk is permanent.** The mic is weak enough that the wake word will
  sometimes miss.
- **Half-duplex on purpose.** Suppressing the uplink while the server speaks
  sidesteps acoustic echo cancellation on a device with one weak microphone.
- **Music has its own audio channel and its own `AudioTrack`.** Merging it into
  the voice channel would still be wrong: that path reports speaking state,
  which closes the mic uplink, so an album playing would leave no way to say
  "pause".
- **Voice and vision processing stay on the host.** The clock, alarms and timers
  run locally so they keep working with the host switched off.

---

## Known limitations

- **The microphone is the real risk.** One mic of the array, low gain. Whether
  far-field wake word is viable is empirical and only answerable on the unit.
- **The host is a single point of failure for voice and vision** — deliberately
  not for the clock.
- **You lose Alexa permanently.** Only a full TWRP backup restores it.
- **Camera frames go out at 720×720, not 768×768.** The sensor's native capture
  is 1280×720 and its centre square is 720; the API recommends 768 rather than
  requiring it, and upscaling would invent detail.
- **Spotify is written but unproven end to end** — see above.
- **One unit, one build.** Nothing here has been validated on a second device, a
  second Android version, or a non-Windows host.

---

## Reference

- Gemini Live API — <https://ai.google.dev/api/live>
- Live API capabilities (audio + video) — <https://ai.google.dev/gemini-api/docs/live-api/capabilities>
- openWakeWord — <https://github.com/dscripka/openWakeWord>
- amonet, `mt8163-checkers` branch — <https://github.com/R0rt1z2/amonet/tree/mt8163-checkers>
- Camera and Bluetooth patches — <https://github.com/jxlarrea/lineageos-echo-show-camera>
- Tailscale APKs (`armeabi-v7a`, no Play Services) — <https://pkgs.tailscale.com>

---

## License

MIT — see [LICENSE](LICENSE).
