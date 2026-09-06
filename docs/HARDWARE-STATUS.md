# Hardware status — Echo Show 5 (gen 1, `checkers`)

> **This is a template. Every cell marked `TO MEASURE` is empty on purpose.**
> Fill it in during hardware bring-up, on the actual unit, with the actual
> command output pasted in. A number copied from a teardown, a forum post, or
> this repo's own handoff is not a measurement — the whole point of this file is
> to record what *this* device does, because two of the numbers here decide how
> the software is built.
>
> Do not delete the `TO MEASURE` markers by guessing. Leave them until the
> device is in front of you.

| | |
|---|---|
| Unit | Echo Show 5, 1st gen (2019), model `H23K37` |
| Codename | `checkers` |
| Bring-up started | `TO MEASURE` (date) |
| Last updated | `TO MEASURE` (date) |
| Filled in by | `TO MEASURE` |

---

## 1. The two numbers that change the design

These are gates, not statistics. Everything downstream waits on them.

### 1.1 Microphone level — **the central technical risk**

HANDOFF §12: *"the microphone is the real technical risk, not the flashing or
the ROM build. Everything downstream of 'can it hear you across the room' is
ordinary engineering; that question is empirical and only answerable on this
unit."*

On LineageOS only one mic of the array is live and the gain is low. **Measure
before writing app config**, per HANDOFF §6 / verification step 6.

```bash
# 30 s at a normal speaking distance, then pull it to the host
adb -s <serial> shell 'arecord -D hw:0,0 -f S16_LE -r 16000 -c 1 -d 30 /sdcard/mic-test.wav' \
  || adb -s <serial> shell 'tinycap /sdcard/mic-test.wav -c 1 -r 16000 -b 16 -T 30'
adb -s <serial> pull /sdcard/mic-test.wav .
```

Then inspect the actual levels — peak and RMS, not a waveform eyeballed in a
player:

```bash
ffmpeg -i mic-test.wav -af volumedetect -f null -    # mean_volume / max_volume
```

| Measurement | Value | Notes |
|---|---|---|
| Distance from mic | `TO MEASURE` | metres, normal speaking voice |
| Peak level (dBFS) | `TO MEASURE` | |
| Mean/RMS level (dBFS) | `TO MEASURE` | |
| Noise floor, quiet room (dBFS) | `TO MEASURE` | |
| Usable SNR | `TO MEASURE` | mean minus noise floor |
| At 3 m across the room | `TO MEASURE` | the far-field question |

**Derived settings — write these into config once measured:**

| Setting | Where | Value |
|---|---|---|
| `MIC_GAIN` | repo-root `.env` | `TO MEASURE` (1.0 is a placeholder, not a calibration) |
| `AudioCapture.VAD_THRESHOLD_RMS` | `EchoTerminal/.../AudioCapture.kt` | `TO MEASURE` (ships at 420) |
| `WAKE_THRESHOLD` | repo-root `.env` | `TO MEASURE` (ships at 0.5) |

**Gate — circle one:**

- [ ] **Far-field viable.** Wake word works across the room. Build as designed.
- [ ] **Near-field only.** Wake word is unreliable beyond ~1 m. The honest
      fallback (HANDOFF §12) is a near-field device where **tap-to-talk is
      primary and wake word is a bonus**. Tap-to-talk is already permanent in
      the client, so this is a documentation and expectation change, not a
      rewrite.

Verdict: `TO MEASURE`

### 1.2 Real RAM

HANDOFF §2 corrects a claim you will encounter: the camera project's
`INSTALL.md` says amonet 2.x gives "full 2 GB RAM access". **That is specific to
`crown` (Echo Show 8) and does not apply to `checkers`.** Do not plan around
2 GB. Confirm on-device.

```bash
adb -s <serial> shell cat /proc/meminfo | head -5
```

| Measurement | Value |
|---|---|
| `MemTotal` | `TO MEASURE` (expect ~1 GB) |
| `MemAvailable`, idle, after stripping LineageOS apps | `TO MEASURE` |
| `MemAvailable` with EchoTerminal running | `TO MEASURE` |
| Peak app RSS during a vision session | `TO MEASURE` |

---

## 2. Capability matrix

From HANDOFF §9, with a column for what this unit actually did.

| Component | Expected after Phases 1–2 | Measured on this unit |
|---|---|---|
| Display + touchscreen | Works | `TO MEASURE` |
| Speakers | Works well | `TO MEASURE` |
| Wi-Fi (incl. WPA3) | Works | `TO MEASURE` |
| Volume + mute buttons | Work | `TO MEASURE` |
| Light sensor | Works | `TO MEASURE` |
| **Microphone** | **Works, degraded** — one mic of the array, low gain. Central risk | `TO MEASURE` (see §1.1) |
| **Camera** | **Works after HANDOFF §5** — 1280×720 JPEG, correct colour/orientation. Single-client, HAL1 | `TO MEASURE` |
| **Bluetooth** | **Works after HANDOFF §5** (patch 0017). Broken in stock LineageOS | `TO MEASURE` |
| Alexa | **Gone permanently.** Only the TWRP backup restores it | n/a |
| Modern Play Store apps | Mostly no — 32-bit, Android 11, 1 GB RAM. Irrelevant to this design | n/a |
| General feel | Reportedly snappier than Fire OS, but deliberate | `TO MEASURE` |

---

## 3. Flash provenance

Getting this wrong is what makes the camera phase impossible, so record it.

```bash
# amonet generation. 1.x means the §5 camera patches CANNOT run.
adb -s <serial> shell 'dd if=/dev/block/by-name/recovery bs=512 count=2 2>/dev/null' \
  | grep -qa microloader && echo "amonet 1.x - UPGRADE REQUIRED" || echo "amonet 2.x - ok"

adb -s <serial> shell getprop ro.product.device         # expect: checkers
adb -s <serial> shell getprop ro.build.version.release  # expect: 11
adb -s <serial> shell getprop ro.build.display.id
adb -s <serial> root && adb -s <serial> shell id        # expect: uid=0(root)
```

| Item | Value |
|---|---|
| Firmware before flashing | `TO MEASURE` (must have been 6.5.7.3 or 6.5.7.1) |
| amonet version used | `TO MEASURE` (**must be ≥ 2.0.1**) |
| §4.4 generation check output | `TO MEASURE` |
| `ro.product.device` | `TO MEASURE` |
| `ro.build.version.release` | `TO MEASURE` |
| LineageOS build id | `TO MEASURE` |
| TWRP backup taken | `TO MEASURE` (date) |
| Backup verified *readable on the host* | `TO MEASURE` — an untested backup is not a backup |
| Backup location | `TO MEASURE` |
| Magisk installed | `TO MEASURE` |
| Custom ROM build date | `TO MEASURE` |
| **Camera shim installed after the last ROM flash** | `TO MEASURE` — see §5 below |

---

## 4. Camera verification

Per HANDOFF §5.8. Run every one; a partial pass is a fail.

```bash
adb -s <serial> shell 'pm list features | grep camera'
#   expect: feature:android.hardware.camera.any
#           feature:android.hardware.camera.front

adb -s <serial> shell 'dumpsys media.camera | grep Orientation'
#   expect: Orientation: 0

adb -s <serial> shell 'logcat -d | grep -cE "startStream fail|deque DISPO fail|submit command block failed"'
#   expect: 0

scripts/camera-preflight.sh <serial>
scripts/camera-test.sh <serial>
```

| Check | Expected | Result |
|---|---|---|
| `pm list features` | `camera.any` + `camera.front` | `TO MEASURE` |
| `dumpsys media.camera` orientation | `Orientation: 0` | `TO MEASURE` |
| Error-line count in logcat | `0` | `TO MEASURE` |
| Preview appears | ~2 s, right-side-up, correct colour | `TO MEASURE` |
| Still capture | ~1 s, 1280×720 JPEG | `TO MEASURE` |
| Lens covered | essentially black frame | `TO MEASURE` |
| Available preview sizes | (chosen by `CameraSource.chooseSize`) | `TO MEASURE` |
| Frame size sent to Gemini | expect 720×720 (see note) | `TO MEASURE` |
| **Open/close 50× (verification step 16)** | no leak, no `cameraserver` wedge | `TO MEASURE` |

> **Note on 720 vs 768.** The API *recommends* 768×768; the sensor's native
> capture is 1280×720, whose centre square is 720. `CameraSource` sends 720×720
> rather than upscaling, because upscaling invents detail. If a preview size
> with a short edge ≥ 768 turns out to be available on this unit, record it
> above — the code will pick it automatically.

---

## 5. The failure that will catch you later

> **Reinstalling the ROM silently breaks the camera until the shim step is
> re-run.** (HANDOFF §5.9, §12.) The symptom is `Number of camera devices: 0`,
> and nothing else looks wrong.

Every time the ROM is reflashed, re-run HANDOFF §5.7 step 3 and log it here:

| Date | ROM flashed | Shim re-run? | Camera verified after |
|---|---|---|---|
| `TO MEASURE` | | | |

---

## 6. Network

| Item | Value |
|---|---|
| Tailscale APK version (`armeabi-v7a`) | `TO MEASURE` |
| Device tailnet name | `TO MEASURE` |
| Server tailnet name | `TO MEASURE` |
| MagicDNS enabled | `TO MEASURE` |
| Always-on VPN enabled | `TO MEASURE` |
| Device pings the PC by MagicDNS name | `TO MEASURE` |
| Tunnel survives a device reboot | `TO MEASURE` |
| Round-trip latency, device → PC | `TO MEASURE` (ms) |

---

## 7. Soak

Verification step 17: 72 hours. 1 GB of RAM is unforgiving of leaks, and the
clock must survive every one of these.

| Observation | Result |
|---|---|
| Start / end time | `TO MEASURE` |
| Reconnects across Wi-Fi blips | `TO MEASURE` (count, all recovered?) |
| Reconnects across server restarts | `TO MEASURE` |
| App RSS at start / end | `TO MEASURE` |
| Clock ever stopped rendering | `TO MEASURE` (must be: no) |
| Clock kept working while the link was down | `TO MEASURE` (must be: yes) |
| `cameraserver` wedged | `TO MEASURE` (must be: no) |
| Unexpected reboots | `TO MEASURE` |

---

## 8. Open observations

Anything surprising, so the next person does not rediscover it.

```
TO MEASURE
```

---

## MEASURED — 2026-09-06 (real unit, LineageOS 18.1 v0.6)

| Slot | Value |
|---|---|
| Unit serial | G0913L0594031A7Q |
| RAM (MemTotal) | 996,988 kB ≈ **974 MiB** (NOT 2 GB — crown-only claim confirmed inapplicable) |
| ROM | lineage-18.1-20260624-UNOFFICIAL-checkers (v0.6), sha256 8a0c7f5d…7db32f |
| Root | Magisk 30.7, `su -c id` → `u:r:magisk:s0` (app-level root CONFIRMED) |

### 1.1 Microphone — MEASURED (near-field verdict)

Method: raw ALSA capture via tinycap returns **pure digital silence** on every PCM
device (ADC switches `Audio_ADC_1..4_Switch` are Off until the Android HAL routes a
use-case — MTK ADSP owns the mics). screenrecord has no `--audio-source=mic` on this
build. **The only valid capture path is the Android audio stack** (AudioRecord): used
`org.lineageos.recorder` sound mode, 31.7 s at arm's length, pulled + analyzed (ffmpeg
decode → numpy RMS per window).

| Metric | Measured | Healthy ref |
|---|---|---|
| Speech level (p90 of 1 s windows) | **−45.6 dBFS** | −30…−20 dBFS |
| Peaks | −25.7 dBFS | −6…−12 dBFS |
| Noise floor (p10) | −63.5 dBFS | — |
| SNR | **~18 dB** | — |
| Per-second range | −43…−63 dBFS (no clipping, no gating artifacts) | — |

**Verdict: mic works; gain is ~20–30 dB low (handoff warning confirmed).** Usable
near-field after the mandatory software gain (+24…+28 dB → MIC_GAIN ≈ 16–24× in
AudioCapture). Far-field (3–4 m) take NOT yet recorded — pending. Wake word:
treat as bonus; tap-to-talk is the reliable interface. Tuning data path exists
(RECORD_AUDIO_DIR) for later calibration.

### Gotchas recorded
- tinycap/tinymix exist; ADC switches default Off; HAL routes mics only during an
  active AudioRecord use-case. Raw ALSA probing is a dead end on this MTK stack.
- LineageOS Recorder output lands in `/sdcard/Music/Sound records/*.m4a` (AAC 256k).
