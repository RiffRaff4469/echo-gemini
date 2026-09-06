# HARDWARE-BRIEF-7 — Physical controls: mic button + camera switch

Field capture (2026-09-06, on-device `getevent`, LOS 18.1 v0.6):
- **Mic button** (top, mic icon) → `event0` "gating" (amazon-gating driver) emits
  **EV_KEY code 0x74 (116 = KEY_POWER)** on press/release. LOS has no specific
  keylayout for "gating" (Vendor 0000/Product 0000) → Generic.kl maps 116 →
  KEYCODE_POWER → **screen turns off**. That's the observed behavior. Root cause
  confirmed: FireOS interpreted this as the privacy mute; LOS sees a power key.
- **Camera shutter switch** → `event6` "gpio-keys" emits **EV_SW code 9
  (SW_CAMERA_LENS_COVER)** 1=covered / 0=open. Android has native framework
  handling for lens-cover switches, so the camera stack already reacts (owner
  saw "something happen" — the app/camera availability changes). This one is
  NOT broken; verify the app shows SHUTTER CLOSED via its existing
  `CameraStatus.SHUTTER_CLOSED` path and keep it.

## Owner requirements (decided 2026-09-06)
- Mic button while a chat/session is active → **end the chat immediately**,
  return to ambient (wake word re-arms). This is the primary semantic.
- Mic button when idle → treat as a privacy toggle: device goes **deaf**
  (wake word disabled, no audio sent anywhere) with a clear on-screen indicator;
  press again to re-enable. (Amazon-style intent; owner can veto — default to
  this.)
- Screen-off-by-button is NOT a feature to preserve (FireOS never slept the
  Show from this button; sleep is timeout-driven). Device sleeps on its own.

## Work items
1. **Keylayout remap (device-side, running ROM):** add
   `/system/usr/keylayout/gating.kl` mapping `key 116 MICMUTE` (LOS keylayout
   lookup is by device name → "gating"). Effect: mic button no longer powers
   the screen off; it becomes KEYCODE_MICMUTE (Android API 30+). Needs root +
   remount (adb root; `mount -o rw,remount /system`), persist, reboot, verify
   with getevent/input. NOTE: the custom ROM (in build) will ship its own
   keylayout — include this mapping in the ROM's device tree too so it survives
   the reflash.
2. **Client handling (EchoTerminal):** in `MainActivity.kt` add `onKeyDown` for
   KEYCODE_MICMUTE: if a session is active → send the server the end-chat /
   session-close (reuse whatever closes a session today; if none exists client-
   side, server protocol may need a small "user_end" envelope — check
   `Link.kt`/`Protocol.kt`/`server/main.py`). If idle → toggle the deaf/privacy
   state: server stops running the wake-word detector for this device + client
   shows a clear MUTED/DEAF indicator (StatusOverlay-style pill; restyle with
   the ambient redesign's language). Press again → re-enable.
3. **Camera switch:** confirm the client's shutter state path shows correctly on
   cover/open (existing `CameraStatus.SHUTTER_CLOSED` + StatusOverlay shutter
   pill). Fix only if broken. No new work expected.
4. Custom-ROM phase: ensure `gating.kl` ships in the device tree + investigate
   the mute **LED** (orange light on press) via the amazon kernel's LED/GPIO —
   defer; nice-to-have.

## Scope rules
- Follow UI-BRIEF-4/5/6 sequencing (this repo has one build worker at a time).
- Server: only the wake-detector pause + any session-close envelope needed.
  Client: MainActivity key handling + indicator. Tests where the server
  changes. NO Compose. armeabi-v7a, minSdk 30.
- Do not touch WSL / ROM build. Do not disturb ambient-UI, chat-end, or Elliot
  wake work beyond the interface points above.

## Deliverable + verification
- Commits with clear messages. Server suite green (`.venv/Scripts/python.exe -m
  pytest server/ -q`), `./gradlew assembleDebug` green (JAVA_HOME
  `C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot`, ANDROID_HOME
  `C:\Users\jaide\Android\Sdk`).
- Device verified live: press mic button during chat → chat ends, screen stays
  on; press when idle → deaf indicator appears, "Elliot" does not trigger; press
  again → recovers. Report ~15 lines + commit SHAs.
