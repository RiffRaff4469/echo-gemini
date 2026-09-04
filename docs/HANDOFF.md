# Echo Show 5 (Gen 1) → Self-Hosted Gemini Appliance

> **Handoff document.** This is written to be executed by an agent with no prior context from the conversation that produced it. Every decision, version constraint, and source URL needed is included. Where something must be verified on the actual hardware rather than assumed, it is called out explicitly.

---

## 1. Handoff brief

### Confirmed device state

| Fact | Value | How it was established |
|---|---|---|
| Device | **Echo Show 5, 1st gen (2019)** | User confirmed |
| Codename | **`checkers`** | Maps to gen 1 |
| Model number | `H23K37` | On the underside label |
| Firmware | **Fire OS 6.5.7.3** | User read it from Settings → Device Options → About |
| Wi-Fi | **Off, and must stay off until flashed** | User confirmed |
| SoC | MediaTek MT8163 (quad Cortex-A53, 64-bit capable) | Teardowns |
| RAM | **1 GB DDR3** — see §2 note | Teardown + maintainer clarification |
| Storage | 8 GB | Teardown |
| Display | 5.5", 960×480 | Spec |
| Camera sensor | **OV9734**, 1 MP | Camera project docs |

6.5.7.3 is one of only two firmware versions the gen 1 exploit supports (the other is 6.5.7.1). **The offline state is protecting that window** — an OTA can patch the LK bootloader and permanently close it. Do not connect to Wi-Fi before Phase 1 completes.

### Goal

Turn the device into three things at once:

1. **An everyday ambient display** — a clock by default, plus arbitrary content pushed via API. **Must keep working locally when the backend is down.**
2. **A Gemini voice terminal** — wake word → conversation.
3. **A Gemini Live *view*** — the camera streams frames so Gemini can see what the device sees.

### Decisions already made (do not re-litigate)

- **Thin-terminal architecture.** The Show captures audio/video and renders a display. A **Windows PC reachable over Tailscale** runs wake word detection, all Gemini Live API calls, and processing. The API key never touches the device.
- **Camera is in scope**, not deferred. This makes building LineageOS from source **mandatory** (§5).
- **Wake word is in scope**, running server-side.
- **Tap-to-talk is retained permanently** as a manual override, not as a temporary v1 shim — the microphone is weak enough that wake word will sometimes miss.
- **No GApps.** Nothing in this design needs Play Services, and on 1 GB of RAM omitting it is the single largest performance win available.

---

## 2. Hardware constraints that drive the design

**1 GB of RAM, 32-bit ARM userspace, Android 11.** Google's Gemini app requires Android 9+ and ≥2 GB RAM, so it cannot run here. Under the thin-terminal architecture it doesn't need to.

> **Correcting a claim you will encounter:** the camera project's `INSTALL.md` says upgrading to amonet 2.x gives "full 2 GB RAM access." **This does not apply to `checkers`.** The maintainer has clarified that the 2 GB unlock is specific to `crown` (Echo Show 8). Echo Show 5 units have 1 GB. Do not plan around 2 GB. Confirm on-device with `cat /proc/meminfo` after flashing and record the real number in `docs/HARDWARE-STATUS.md`.

**The microphone is the single largest technical risk.** On LineageOS, only one mic of the array is live and gain is low. This is measured in Phase 4 *before* app code is written, because the result decides whether this is a far-field or near-field device.

**Camera constraints that shape the app:**
- Legacy HAL1 → **single-client camera access only.** One consumer at a time, ever.
- **Never SIGKILL or force-stop `cameraserver` while streaming.** It causes an IOMMU livelock requiring a physical power cycle. This is a hard app-lifecycle constraint, not a warning to skim.
- Native still capture is **1280×720 JPEG**, ~1 s per shot, preview up in ~2 s.
- There is a **physical privacy shutter**. Patches 0015/0016 keep the camera enumerated when the latch is engaged rather than crashing.

**Gemini Live video input spec:** discrete JPEG frames at **max 1 FPS**, **768×768 recommended**, sent as base64 in `realtimeInput.video` with `mimeType: "image/jpeg"`. Session-level `media_resolution` accepts `low`/`medium`/`high`. **1 FPS at 768×768 is comfortably within this hardware's ability** — downscale from the native 1280×720. This is why Live view is viable despite the modest SoC.

---

## 3. Architecture

```
┌─ Echo Show 5 (LineageOS 18.1 custom build, rooted, kiosk) ──┐
│                                                              │
│  EchoTerminal.apk  (Kotlin, minSdk 30, armeabi-v7a)          │
│    ├─ Ambient layer   LOCAL clock — zero network dependency  │
│    │                  + WebView overlay for pushed content   │
│    ├─ AudioCapture    16 kHz mono PCM16 + gain + VAD gate    │
│    ├─ AudioPlayback   24 kHz mono PCM16 + barge-in flush     │
│    ├─ CameraSource    1 FPS JPEG @768×768, single-client,    │
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

**Why the device dials out:** one outbound WebSocket, kept alive, multiplexing audio up, video up, audio down, and display commands down. No inbound ports on the device, no NAT/firewall problems, and reconnection logic lives in exactly one place.

**Why a full server proxy rather than direct-to-Google with ephemeral tokens:** the usual objection is added latency, but the proxy is a local machine one Tailscale hop away, so the cost is a few milliseconds. In exchange: the API key stays on the PC, heavy lifting runs on real hardware, and wake-word tuning iterates without touching the device.

---

## 4. Phase 1 — Unlock and flash stock LineageOS

No disassembly, no soldering, no test points.

### 4.1 Host matrix — which machine for which task

This distinction matters and is easy to get wrong:

| Task | Host | Why |
|---|---|---|
| **amonet BROM exploit** | **Ubuntu live USB, booted on the Windows PC** (recommended) | Needs raw, low-latency USB. The device **re-enumerates as different USB identities** across BROM → preloader → fastboot, so WSL2 + `usbipd-win` is fragile in exactly the wrong window. macOS works but has documented timing stalls needing retries |
| ROM build | **WSL2 on the Windows PC** | No USB involved. See §5.1 |
| `adb sideload`, `fastboot flash`, repo scripts | Windows, macOS, or WSL2+usbipd | Steady-state adb/fastboot is far more forgiving than the BROM window |

**Do not attempt the amonet exploit through WSL2.** Everything *after* the exploit is fine anywhere.

### 4.2 Prerequisites

- Android SDK Platform Tools (`adb`, `fastboot`), Python 3
- **A data-capable micro-USB cable.** Charge-only micro-USB cables are extremely common and are the most frequent cause of "the exploit doesn't work" reports. **Verify with `adb devices` before starting.**
- **The original barrel-jack 15 W PSU.** The micro-USB port is data/service only and will not power the device.

### 4.3 Sequence

1. **Keep Wi-Fi off** throughout.
2. **Reconfirm firmware is 6.5.7.3** (Settings → Device Options → About). Go/no-go.
3. **Get amonet, branch `mt8163-checkers`:** <https://github.com/R0rt1z2/amonet/tree/mt8163-checkers>
   - **Use amonet 2.0.1 or newer.** This is a hard requirement: the camera patches in §5 **cannot run on devices unlocked with amonet 1.x**, and 1.x uses a different partition layout. Getting this wrong means redoing Phase 1 entirely.
   - amonet 2.0 reworked the unlock around a stronger Preloader exploit, dropped microloader/lk-payload, ships TWRP 3.7.0_9-0, and adds **Preloader USBDL recovery mode** — this is your unbrick path.
   - **The `cronos` (gen 2) package will bootloop this device.** Verify the branch.
4. **Enter fastboot:** hold **Volume Down + Volume Up + Mute** while powering on.
5. **Run the amonet exploit** from the Linux host.
6. **Flash `boot-root.img`** for `checkers` matching 6.5.7.x. Device- and firmware-specific; **the wrong file bootloops.** Verify checksums against the source thread.
7. **Flash TWRP, take a full partition backup, and copy it off to the host.** Non-negotiable — it is the only clean route back to working Fire OS. **An untested backup is not a backup:** confirm it is readable on the host before continuing.
8. **Flash R0rt1z2's stock unofficial LineageOS 18.1 for `checkers`.** These devices use a **system-as-root** layout, so several ordinary TWRP flows fail and the guides use TWRP's CLI tools instead — follow the device thread literally.
9. **Skip GApps.**
10. **Install Magisk** for root (flash, then install the app).

> **Why flash stock LineageOS first, before the custom camera build?** It validates the entire exploit-and-flash chain and gives a working device *before* anyone spends 4–8 hours on a source build. It also matches the camera guide's stated prerequisite (a device already running unofficial LineageOS 18.1 with adb root). Treat this as a checkpoint, not wasted work.

### 4.4 Verify the unlock generation

The camera work depends on this. Run:

```bash
adb -s <serial> shell 'dd if=/dev/block/by-name/recovery bs=512 count=2 2>/dev/null' \
  | grep -qa microloader && echo "amonet 1.x - UPGRADE REQUIRED" || echo "amonet 2.x - ok"
```

Also confirm the baseline:

```bash
adb -s <serial> shell getprop ro.product.device        # expect: checkers
adb -s <serial> shell getprop ro.build.version.release # expect: 11
adb -s <serial> root && adb -s <serial> shell id       # expect: uid=0(root)
cat /proc/meminfo                                      # record actual RAM
```

### 4.5 Bricking risk

Real but recoverable. The entry point is a *BootROM/Preloader* exploit, so recovery does not depend on a working bootloader — amonet 2.x ships Preloader USBDL recovery mode, and the XDA threads document unbrick procedures. The genuinely unrecoverable paths are physical damage and taking an OTA before starting.

**Backup rule:** never restore a pre-2.x boot backup onto a 2.x device — the partition layout differs.

---

## 5. Phase 2 — Build LineageOS 18.1 with camera + Bluetooth

**This phase is mandatory because camera is in scope.** The camera does not work in stock LineageOS 18.1; neither does Bluetooth. Both are fixed by <https://github.com/jxlarrea/lineageos-echo-show-camera>, which distributes patches and scripts — **no proprietary binaries** — so a full source build is required.

What the patches deliver on `checkers`: live preview with correct colors, still capture at full resolution, auto-exposure, adaptive white balance, lens shading correction, correct orientation, privacy-latch handling, and a working Bluetooth adapter.

### 5.1 Build host

**WSL2 on the Windows PC**, with the source tree **inside the WSL2 ext4 filesystem** — *not* under `/mnt/c`, where case-insensitivity and I/O performance will break or cripple the build. No USB is involved in building, which is why WSL2 is fine here and unsuitable for Phase 1.

- **Disk:** ~150 GB per the project docs; **provision 250 GB** to be safe with `ccache` and `out/`. The WSL2 virtual disk must be able to grow to this.
- **RAM:** 16 GB minimum, 32 GB strongly preferred.
- **Ubuntu** (20.04-era userspace suits `lineage-18.1`).
- macOS is **not** a viable build host for this branch.

### 5.2 Tree setup

```bash
export CAMERA_DEVICE=checkers
mkdir -p ~/lineage-18.1 && cd ~/lineage-18.1
repo init -u https://github.com/LineageOS/android.git -b lineage-18.1 --depth=1
git clone https://github.com/amazon-oss/local_manifests.git -b lineage-18.1 \
    .repo/local_manifests
```

**Two fixes required before syncing.**

Remove a stale manifest entry — create `.repo/local_manifests/zz-local-fixes.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remove-project name="android_hardware_mediatek_mt66xx" />
</manifest>
```

Then sync and add the missing toolchain:

```bash
repo sync -c --no-clone-bundle --no-tags -j8     # -j8, not max: avoids connection drops
git clone --depth 1 -b lineage-21.0 \
    https://github.com/mt8163/android_prebuilts_linaro prebuilts/linaro
./patches/apply.sh
. build/envsetup.sh
breakfast lineage_checkers-userdebug
```

Also clone the camera project:

```bash
git clone https://github.com/jxlarrea/lineageos-echo-show-camera ~/lineageos-echo-show-camera
```

Note `patches/local-manifest-fixes.xml` in that repo carries additional manifest fixes the work needs.

### 5.3 Kernel patches — `checkers` only

```bash
cd ~/lineage-18.1/kernel/amazon/mt8163-4.9

# All Echo Show devices
patch -p1 < ~/lineageos-echo-show-camera/patches/0001-imgsensor-use-the-Amazon-struct-layouts-on-all-echo-show.patch

# checkers ONLY — OV9734 vertical flip
patch -p1 < ~/lineageos-echo-show-camera/patches/0014-*.patch

# checkers ONLY — privacy latch handling at power-on
patch -p1 < ~/lineageos-echo-show-camera/patches/0015-*.patch

# checkers ONLY — keep camera enumerated with privacy latch engaged
patch -p1 < ~/lineageos-echo-show-camera/patches/0016-*.patch
```

**Skip the OV02B10 driver steps (guide steps 3.2 and 3.3) — those are `cronos`-only.** The OV9734 driver is already in the tree and is selected by the patches above.

### 5.4 ROM patches

```bash
cd ~/lineage-18.1/hardware/interfaces
patch -p1 < ~/lineageos-echo-show-camera/patches/0005-*.patch

cd ~/lineage-18.1/frameworks/av
patch -p1 < ~/lineageos-echo-show-camera/patches/0006-*.patch

cd ~/lineage-18.1
patch -p1 < ~/lineageos-echo-show-camera/patches/0011-*.patch
patch -p1 < ~/lineageos-echo-show-camera/patches/0012-*.patch
patch -p1 < ~/lineageos-echo-show-camera/patches/0013-*.patch   # disables cameraserver at boot
patch -p1 < ~/lineageos-echo-show-camera/patches/0017-*.patch   # Bluetooth, all devices
```

Patch 0013 keeps `cameraserver` off at boot so it cannot crash before the shim is installed. Patch 0017 is what fixes Bluetooth.

### 5.5 Vendor blobs

```bash
export CAMERA_DEVICE=checkers
cd ~/lineageos-echo-show-camera

cat patches/camera-proprietary-files.txt \
    >> ~/lineage-18.1/device/amazon/$CAMERA_DEVICE/proprietary-files.txt
(cd ~/lineage-18.1/device/amazon/$CAMERA_DEVICE && ./setup-makefiles.sh)

scripts/fetch-camera-blobs.sh                        # 45 libs, ~17 MB, from checkers dump
scripts/install-blobs-to-tree.sh ~/lineage-18.1
scripts/install-private-dpframework.sh ~/lineage-18.1  # from cronos dump, all devices
scripts/patch-shim-needed.sh ~/lineage-18.1
```

Verify before building:

```bash
ls -l ~/lineage-18.1/vendor/amazon/checkers/proprietary/vendor/lib/libdpframework_cam.so
# expect ~333 KB

~/lineage-18.1/prebuilts/extract-tools/linux-x86/bin/patchelf-0_9 --print-needed \
    ~/lineage-18.1/vendor/amazon/checkers/proprietary/vendor/lib/libcam.client.so \
    | grep -E 'libcamera_shim|libdpframework'
# expect BOTH listed
```

### 5.6 Build

```bash
cd ~/lineage-18.1
. build/envsetup.sh
lunch lineage_checkers-userdebug
export MKE2FS_CONFIG=$PWD/system/extras/ext4_utils/mke2fs.conf
mka bacon
```

**Validate by reading the log, not the exit code** — grep for `FAILED:` and confirm `#### build completed successfully ####`. Then:

```bash
cd ~/lineageos-echo-show-camera
scripts/verify-build.sh ~/lineage-18.1
```

### 5.7 Flash — order is critical

```bash
# 1. ROM first
adb reboot recovery
adb sideload out/target/product/checkers/lineage-18.1-*.zip

# 2. Boot image second, from booted Android
cd ~/lineageos-echo-show-camera
scripts/flash-boot.sh <serial> ~/lineage-18.1
#   or: fastboot flash boot ~/lineage-18.1/out/target/product/checkers/boot.img && fastboot reboot

# 3. On-device shim — DO NOT open the camera app before this completes
shims/libcmdqevent/build.sh <serial>
scripts/install-cmdq-event-shim.sh <serial>
adb -s <serial> reboot
```

**Skip guide steps 9.2 and 9.3** (`patch-awb-d65.sh`, `patch-obc-pedestal.sh`) — those are `cronos` white-balance and black-level corrections. Stock tuning is already correct for OV9734.

**Only ever flash boot via `scripts/flash-boot.sh` or `fastboot flash boot`.**

**If the ROM is ever reinstalled later, the shim step must be re-run.** This is the most common post-install failure.

### 5.8 Camera verification

```bash
adb -s <serial> shell 'pm list features | grep camera'
# expect: feature:android.hardware.camera.any
#         feature:android.hardware.camera.front

adb -s <serial> shell 'dumpsys media.camera | grep Orientation'
# expect: Orientation: 0

adb -s <serial> shell 'logcat -d | grep -cE "startStream fail|deque DISPO fail|submit command block failed"'
# expect: 0

scripts/camera-preflight.sh <serial>
scripts/camera-test.sh <serial>
```

Functional check: preview appears in ~2 s, right-side-up, correct colors; a photo completes in ~1 s producing a 1280×720 JPEG; covering the lens produces an essentially black frame.

### 5.9 Camera troubleshooting

| Symptom | Cause / fix |
|---|---|
| "Number of camera devices: 0" | Run `scripts/camera-preflight.sh`. Most common cause: ROM reinstalled after the shim step — re-run §5.7 step 3 and reboot |
| Uniform green preview | Frames not delivered. Check `LD_PRELOAD` in `/system/etc/init/cameraserver.rc`. If logs show `submit command block failed(-1)`, the legacy-interface `libdpframework` was installed — re-run `install-private-dpframework.sh` |
| Blacks grey/hazy | Should not occur on OV9734. If it does, you likely applied the `cronos`-only tuning patches — undo them |
| Device unresponsive | `cameraserver` was force-killed while streaming → IOMMU livelock. Requires physical power cycle. **Design the app so this cannot happen** |
| Hang at vendor logo | An amonet 1.x boot image was flashed. Reflash the plain build `boot.img` via fastboot or TWRP |

### 5.10 Known residual camera limitations

Scene-to-scene color variance ~±10%; greenish cast in dim mixed lighting; IR leakage artifacts in low-sun conditions; **single-client access only** (HAL1). None block Live view at 1 FPS.

---

## 6. Phase 3 — Device baseline

- **Tailscale:** sideload the official `armeabi-v7a` APK from <https://pkgs.tailscale.com> (supports 32-bit ARM; **does not require Play Services**). Authenticate, enable MagicDNS, confirm the Show reaches the Windows PC by tailnet name, and enable always-on VPN so the tunnel survives reboots.
  - *Fallback:* with root, run `tailscaled` directly from the static ARM binary in userspace-networking mode.
  - *Simplification:* if both machines are on the same LAN, plain LAN networking works. Tailscale buys resilience and remote access.
- **Measure the microphone — this is a gate, not a formality.** Record 30 s via `adb` at normal speaking distance, pull the WAV, inspect actual levels. The result sets the software gain constant and determines whether far-field wake word is realistic. Record findings in `docs/HARDWARE-STATUS.md`.
- **Confirm** touchscreen, Wi-Fi, speaker tone, light sensor, Bluetooth (now fixed), camera (§5.8).
- **Record actual free RAM** (`cat /proc/meminfo`) and strip unneeded LineageOS apps, animations, and background services.

---

## 7. Phase 4 — Server (`echo-server`, Python, Windows)

**Responsibilities**

- **WebSocket hub** — accept the device's persistent connection; heartbeat, reconnect, multiplex audio/video/display channels.
- **Wake word** — **openWakeWord** (ONNX Runtime) over the inbound audio stream.
- **Gemini Live session management** — on wake, open a session via the `google-genai` SDK; stream audio in; stream model audio back. **Close sessions on idle** — Live sessions bill for the duration they are open.
- **Video control** — request frames from the device only when a session wants vision; forward as `realtimeInput.video`, `mimeType: "image/jpeg"`, ≤1 FPS. Set session `media_resolution` appropriately.
- **Display API** — `POST /display` accepts content and pushes it down the existing WebSocket. This is the "show anything via API" surface.
- **Conversation state, logging, system-instruction management.**

**Why wake word runs server-side:** it avoids the 32-bit Android native-library problem, costs the Show zero CPU, and — decisively — allows tuning against a weak single microphone by iterating on logged audio without reflashing anything.

**Bandwidth:** audio is 256 kbit/s uncompressed, gated to ~zero when idle by device-side VAD. Video at 1 FPS / 768×768 JPEG is roughly 0.5–1 Mbit/s while active. Both are trivial on a LAN or local tailnet. (Optimization, not v1: Android 10+ has a hardware Opus encoder via `MediaCodec`.)

**Windows operational details — easy to lose a day to:**
- Run as a real service (NSSM or a Task Scheduler at-boot task), not a terminal window.
- **Disable sleep/hibernate and USB selective suspend.** An always-on backend that sleeps is not one.
- Allow the listener through **Windows Firewall on the Tailscale interface** specifically.
- `GEMINI_API_KEY` lives in the service environment. Never in the repo, never on the device.
- Add a **shared-secret header** on the WebSocket handshake. The tailnet is the real boundary; defense in depth is cheap.
- **Confirm the current Live API model id** against <https://ai.google.dev/api/live> at build time — that model line has been renamed repeatedly. Treat it as configuration, not a constant.

---

## 8. Phase 5 — Client (`EchoTerminal.apk`)

One Kotlin app, set as the device launcher so it boots straight in. `minSdk 30`, `armeabi-v7a`.

### 8.1 Ambient layer — the day-to-day clock

The "must work when the backend is down" requirement decides this design:

- **A native, locally-rendered clock is the base layer and always runs** — time, date, subtle connection-status indicator, **zero network dependency**. If the Windows PC is off, the Show is still a clock.
- **A WebView sits above it for pushed content.** On a display command the WebView renders; on expiry or link loss it fades back to the local clock. WebView is the right choice for the push surface specifically because it makes "anything via API" true — weather, calendar, photos, transit, dashboards — without shipping a new APK per card type.
- **Display command schema:** `{type, payload, duration, priority}` with `type` ∈ text | html | image | timer. Keep it small and versioned.
- **Auto-dim at night** via the working light sensor. The panel is LCD, so burn-in is not a concern, but a clock that blazes at 3 a.m. is its own problem.
- `FLAG_KEEP_SCREEN_ON`, `RECEIVE_BOOT_COMPLETED`, launcher replacement, root-suppressed setup-wizard remnants.

### 8.2 Voice layer

- **Capture:** `AudioRecord`, 16 kHz mono PCM16, ~20 ms frames, **software gain applied** (not optional given the mic), then an **on-device VAD gate** so silence is never streamed.
- **Uplink:** stream gated frames over the WebSocket. The device makes no judgement about meaning.
- **Downlink:** 24 kHz PCM16 → `AudioTrack`. Flush immediately on an interrupt message (barge-in).
- **Echo:** suppress uplink while the server is speaking. Half-duplex deliberately sidesteps acoustic echo cancellation on a device with one weak mic. Revisit only if conversation feels unnatural.
- **UI states:** idle / listening / thinking / speaking, legible across a room on a 960×480 panel, with **tap-anywhere-to-talk as a permanent manual override**.
- **Reconnect:** exponential backoff. The ambient clock must never block on link state.

### 8.3 Camera layer — Gemini Live view

Design directly around the HAL1 constraints in §2:

- **Open on demand, close cleanly.** The camera is opened only when the server requests vision and is released the moment the session ends.
- **Single client, enforced in-app.** Serialize all camera access behind one owner object. Never let two paths open it.
- **Never force-stop `cameraserver`.** No `killProcess`, no restart-on-error shortcuts. On camera error, back off and surface the failure — do not attempt aggressive recovery, which risks the IOMMU livelock that requires a physical power cycle.
- **Capture at ≤1 FPS**, downscale 1280×720 → 768×768, JPEG encode, base64, send as a video frame message. Do not exceed 1 FPS — the API caps there and the extra frames are wasted work.
- **Handle the physical privacy shutter gracefully.** Patches 0015/0016 keep the camera enumerated when the latch is engaged, so frames will be black rather than erroring. Detect near-black frames and tell the user the shutter is closed rather than silently sending black images to Gemini.
- **Show an unmistakable on-screen indicator whenever frames are being sent.** A camera in a home streaming to a cloud model must be visibly doing so.

---

## 9. Capability matrix (after Phases 1–2)

| Component | Status |
|---|---|
| Display + touchscreen | Works |
| Speakers | Works well |
| Wi-Fi (incl. WPA3) | Works |
| Volume + mute buttons | Work |
| Light sensor | Works |
| **Microphone** | **Works, degraded** — one mic of the array, low gain. Central risk; measure in Phase 3 |
| **Camera** | **Works after §5** — 1280×720 JPEG, correct color/orientation. Single-client, HAL1 |
| **Bluetooth** | **Works after §5** (patch 0017). Broken in stock LineageOS |
| Alexa | **Gone permanently.** Only the TWRP backup restores it |
| Modern Play Store apps | Mostly no — 32-bit, Android 11, 1 GB RAM. Irrelevant to this design |
| General feel | Reportedly snappier than Fire OS, but deliberate. Not a tablet |

---

## 10. Proposed repository layout

```
echo-gemini/
├── README.md                      # runbook: flash → build → server → client
├── docs/
│   ├── FLASHING.md                # Phase 1 expanded: checksums, USBDL unbrick
│   ├── ROM-BUILD.md               # Phase 2 expanded: exact patch list for checkers
│   └── HARDWARE-STATUS.md         # capability matrix + MEASURED mic + RAM numbers
├── server/
│   ├── main.py                    # WebSocket hub + HTTP display API
│   ├── wake.py                    # openWakeWord over inbound audio
│   ├── gemini_live.py             # Live API session lifecycle, audio + video in
│   ├── protocol.py                # shared message schema (mirrors client)
│   ├── requirements.txt
│   └── install-service.ps1        # NSSM / Task Scheduler registration
└── EchoTerminal/
    ├── app/build.gradle           # minSdk 30, armeabi-v7a
    └── app/src/main/java/.../
        ├── MainActivity.kt        # launcher, layer switching
        ├── AmbientClock.kt        # native local clock — NO network dependency
        ├── PushSurface.kt         # WebView overlay for server content
        ├── Link.kt                # persistent WebSocket, backoff, heartbeat
        ├── AudioCapture.kt        # AudioRecord + gain + VAD gate
        ├── AudioPlayback.kt       # AudioTrack + barge-in flush
        └── CameraSource.kt        # single-owner, 1 FPS, safe lifecycle
```

---

## 11. Verification

Ordered so each stage is independently testable and failures localize.

1. **Firmware gate** — 6.5.7.3 confirmed, Wi-Fi still off.
2. **Cable test** — `adb devices` sees the Show before attempting the exploit.
3. **Backup drill** — TWRP backup taken, copied to host, and *readable there*.
4. **amonet generation check** — §4.4 reports `amonet 2.x - ok`.
5. **Stock LineageOS smoke test** — boots; touchscreen, Wi-Fi, speakers respond. Record real RAM.
6. **Mic measurement** — record via adb, inspect waveform, write the gain constant into `HARDWARE-STATUS.md`. **Gate:** determines near-field vs far-field design.
7. **ROM build** — `#### build completed successfully ####` plus `verify-build.sh` passing.
8. **Camera verification** — all of §5.8, including the lens-covered black-frame test.
9. **Bluetooth** — adapter holds ON without crash-looping; a BLE scan completes without `SCAN_FAILED_INTERNAL_ERROR`.
10. **Tailscale** — Show pings the Windows PC by MagicDNS name; tunnel survives a device reboot.
11. **Server in isolation** — before involving the device, drive the server from a desktop script: feed a WAV, confirm wake word fires, confirm a Live session opens and returns audio, confirm a test JPEG is accepted as video input. Proves key, quota, and model id while debugging is cheap.
12. **Ambient layer alone** — deploy the app with the server **stopped**. Confirm the clock renders, survives reboot, and shows a clear disconnected state. *This is the explicit user requirement; test it by making it fail.*
13. **Full voice loop** — wake word → speech → Gemini → audio out. Watch `adb logcat` beside server logs.
14. **Full vision loop** — ask a question about something held in front of the camera. Confirm frames flow at ≤1 FPS, the on-screen indicator appears, and the camera is released cleanly afterward.
15. **Privacy shutter test** — close the physical shutter mid-session; confirm graceful handling, no crash, no black frames silently sent.
16. **Camera stress** — open/close the camera 50 times in a row. Confirm no leak and no `cameraserver` wedge. This is the highest-consequence failure mode in the build.
17. **72-hour soak** — reconnects across Wi-Fi blips and server restarts, no memory climb (1 GB is unforgiving), clock never dies even when the link does.

---

## 12. Risks and open questions

- **The microphone is the real technical risk**, not the flashing or the ROM build. Everything downstream of "can it hear you across the room" is ordinary engineering; that question is empirical and only answerable on this unit. Phase 3 answers it before app code is written. If it measures poorly, the honest fallback is a near-field device where tap-to-talk is primary and wake word is a bonus.
- **`cameraserver` livelock is the highest-consequence software failure.** It requires physically power-cycling the device. §8.3's lifecycle rules exist specifically to prevent it and should not be relaxed for convenience.
- **amonet 2.0.1+ must be used in Phase 1** or Phase 2 cannot proceed. Verify with §4.4 before investing in a build.
- **The ROM build is a real time investment** — 150–250 GB of disk and hours of compilation. Phase 1's stock-LineageOS checkpoint exists so hardware problems surface before that cost is paid.
- **Reinstalling the ROM later silently breaks the camera** until the shim step is re-run. Document this prominently in the repo README.
- **You lose Alexa permanently**, reversible only via the TWRP backup.
- **1 GB of RAM caps ambition on the device.** The thin-terminal architecture is the answer; resist moving logic back onto the Show later.
- **The Windows PC is a single point of failure for voice and vision — deliberately not for the clock.** Verification step 12 tests that boundary explicitly.
- **Gemini Live bills by session duration**, so idle-close on the server is cost control, not polish.
- **A camera in a home streaming to a cloud model deserves visible indication.** The physical shutter is the user's hard guarantee; the on-screen indicator is the software one.

---

## 13. Reference index

**Exploit and ROM**
- amonet (use 2.0.1+), `mt8163-checkers` branch — <https://github.com/R0rt1z2/amonet/tree/mt8163-checkers>
- XDA — unlock/root/TWRP/unbrick, Echo Show 5 gen 1 (`checkers`) — <https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-show-5-1st-gen-2019-checkers.4762900/>
- XDA — LineageOS 18.1 for `checkers` — <https://xdaforums.com/t/rom-unofficial-11-checkers-lineageos-18-1-for-the-amazon-echo-show-5-2019.4763475/>
- Confirmed-working jailbreak notes — <https://github.com/dallanwagz/echo-show-jailbreak/blob/main/docs/JAILBREAK-SHOW5.md>
- Home Assistant–oriented jailbreak guide — <https://www.derekseaman.com/2025/11/home-assistant-hacking-your-echo-show-5-and-8.html>

**Camera and Bluetooth**
- Project root — <https://github.com/jxlarrea/lineageos-echo-show-camera>
- `docs/INSTALL.md` — step-by-step, per-device
- `docs/building.md` — LineageOS tree setup
- `docs/findings.md` — reverse-engineering record, including dead ends
- `docs/handoff-takepicture.md` — capture-path debugging example
- Kernel source: `amazon-oss/android_kernel_amazon_mt8163`
- Manifests: `amazon-oss/local_manifests`
- Toolchain: <https://github.com/mt8163/android_prebuilts_linaro> (branch `lineage-21.0`)

**Gemini**
- Live API WebSockets reference — <https://ai.google.dev/api/live>
- Live API getting started (WebSockets) — <https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket>
- Live API capabilities (audio + video) — <https://ai.google.dev/gemini-api/docs/live-api/capabilities>
- Endpoint: `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent`
- Client messages carry exactly one of `setup`, `clientContent`, `realtimeInput`, `toolResponse`
- Audio in: raw PCM16, 16 kHz, mono, little-endian. Audio out: 24 kHz
- Video in: JPEG, **max 1 FPS**, 768×768 recommended, `realtimeInput.video`, `mimeType: "image/jpeg"`

**Networking**
- Tailscale Android APKs (`armeabi-v7a`, no Play Services) — <https://pkgs.tailscale.com>
