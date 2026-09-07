# HARDWARE-BRIEF-7 (v2) — Physical controls: mic button = talk, hold = mute + LED; screen taps become UI-only

Repo: echo-gemini. Read `docs/HANDOFF.md` first (architecture + hard rules). This brief
**v2 (owner revision 2026-09-07) SUPERSEDES the 2026-09-06 spec** in earlier copies of
this file: the mic button is no longer a deafen-toggle, and screen taps no longer start
or stop conversations. That decision came out of the owner's Nest-Hub UI overhaul plan
(see `ROADMAP.md` → UI overhaul queue; sibling briefs `CLOCK-BRIEF-STOPWATCH.md`,
`UI-BRIEF-14-LAYOUT-ENGINE.md`, `UI-BRIEF-15-MUSIC-FOCUS.md`, `UI-BRIEF-16-VISUAL-ANSWERS.md`).

## Field capture (verified 2026-09-06, LOS 18.1, do not re-investigate)

- **Mic button** (top, mic icon) → `/dev/input/event0` "gating" (amazon-gating driver)
  emits `EV_KEY` code 0x74 (116 = KEY_POWER) on press/release. LOS has no keylayout
  named "gating" (Vendor 0000/Product 0000) → Generic.kl → KEYCODE_POWER → **screen
  turns off**. Keylayout lookup is by device name → drop
  `/system/usr/keylayout/gating.kl` containing `key 116 MICMUTE`, reboot. Custom ROM
  must ship this file in its device tree (current custom ROM does NOT — see work item 1).
- **Camera shutter switch** → `/dev/input/event6` "gpio-keys" emits `EV_SW` code 9
  (`SW_CAMERA_LENS_COVER`, 1=covered). Framework handles it natively; keep the existing
  `CameraStatus.SHUTTER_CLOSED` pill path. Verify only, no new work.
- A small **orange light reportedly fires on button press** under FireOS (field note).
  Its node under LOS is UNKNOWN — work item 4 is the discovery spike. Do not assume it
  exists until measured.

## Owner requirements — the input model (v2, decided 2026-09-07)

1. **Screen taps no longer start or stop conversations.** The screen is for UI only:
   widgets, chips, pages, option rows. Server must STOP treating an idle tap as
   "start a chat" and a mid-chat tap as "stop" (the v1.4 semantics from
   `FIX-BRIEF-11-TAP-END.md` are retired). Wake word becomes the primary trigger;
   the mic button is the manual override.
2. **Mic button — short press (< 700 ms, judged on release)** = talk toggle:
   - idle → start a conversation (same effect the old idle-tap had);
   - listening/thinking/speaking → end it immediately;
   - muted → **unmute AND start talking** (owner default (a), 2026-09-07 discussion —
     confirm at dispatch; the alternative (b) unmute-only was not chosen).
3. **Mic button — hold (≥ 1 s, fire AT the threshold while still held)** = mute toggle:
   - ends any active session; device goes **deaf**: wake detector paused server-side,
     no audio sent anywhere; **LED on** + on-screen Muted chip;
   - hold again → unmute (wake detector resumes, LED off);
   - mute state **persists across reboot** (SharedPreferences; re-assert on boot,
     LED on if muted — privacy expectation from FireOS behavior);
   - release in the 700 ms–1 s window after a hold fired changes nothing (no cancel).
4. **Wake word "hey jarvis"** (server-side openWakeWord) unchanged — ambient trigger.
5. Screen-off-by-this-button is NOT a feature (device sleeps on its own timeout).

## Protocol (bump minor version → v1.6, keep v1 framing; see `server/protocol.py`)

- device → server `button {action: "talk_toggle" | "mute"}` — the client classifies
  duration locally and sends the semantic action (server is NOT in the key-timing loop).
- server → device `mute {on: bool}` — for voice-initiated mute ("Jarvis, go mute"
  → Gemini tool `set_mute`). Device applies local gate + LED + chip. Mute state is
  **owned on-device** (works with the PC off, same principle as alarms); server mirrors
  it (pause/resume wake detector, mic gate).
- server: REMOVE idle-tap-opens-session handling and any screen-tap stop path. Legacy
  `stop`/`tap` wire messages may stay decode-compatible but no screen path sends them.
- Tests: wire round-trips for `button` and `mute`; wake-detector pause/resume on mute;
  idle tap no longer opens a session.

## Client (EchoTerminal, Kotlin — armeabi-v7a, minSdk 30, no Compose)

1. **Keylayout remap (running ROM):** root + `mount -o rw,remount /system`, add
   `/system/usr/keylayout/gating.kl` (`key 116 MICMUTE`), reboot, verify with
   `getevent` + a KEYCODE_MICMUTE log. **Also add the file to the custom-ROM device
   tree** so the next ROM ships it (dirty-flash swaps currently lose the remap).
2. **MainActivity key handling:** `onKeyDown`/`onKeyUp` for KEYCODE_MICMUTE; measure
   down→up duration (eventTime). `< 700 ms` on release → send `button talk_toggle`.
   `≥ 1 s` down (handler on down, cancel if released early) → send `button mute` and
   apply the local mute transition at the threshold.
3. **Local mute state** (device-owned): stop the audio uplink (AudioCapture) + gate;
   LED via the node from work item 4; Muted chip in StatusOverlay/ambient (restyle
   later with UI-BRIEF-14 tokens); notify server (`button mute`). Apply server
   `mute` commands the same way. Persist + re-assert on boot.
4. **Remove tap-to-chat wiring:** WebView `EchoNative.tap()` no longer starts a
   session (MainActivity `onTap`/document-click path); update the ambient home hint
   ("Tap to talk" → something like "say hey jarvis · press mic" — interim text, final
   copy arrives with UI-BRIEF-14).
5. **LED discovery spike (root adb, running ROM — do before any kernel work):**
   `ls /sys/class/leds`; `find /sys -iname '*led*' -o -iname '*rgb*'`; grep
   `kernel/amazon/mt8163-4.9/arch/arm64/boot/dts/mediatek/checkers.dtsi` for
   gpio-leds / led nodes; check whether the gating driver exposes a brightness node.
   Outcomes: (a) LED-class/sysfs node exists → app sets it (root) on mute;
   (b) raw GPIO only → kernel patch adding a LED-class entry, built as a
   **bootimage-only** change (`mka bootimage` ~15 min, flash boot.img only — same loop
   as the touch-axis fix) — coordinate with the ROM/camera lane owner first, one kernel
   lane at a time; (c) no controllable LED found → on-screen Muted chip is the
   indicator and this brief says so explicitly (do NOT invent a GPIO).

## Scope rules

- One worker per repo; if a worker is mid-run on this brief's OLD spec, stop/repoint it
  before starting (git status + process check first — always).
- Don't disturb the camera/ROM lane, the ambient WebView layout work, or the audio
  pipeline beyond the mute gate above. Tests only where the server changes.
- Commit in slices (keylayout docs+device note, client handling, protocol, server
  tools, tests).

## Deliverable + verification

- Server suite green: `.venv/Scripts/python.exe -m pytest server/ -q`.
- APK compiles: `cd EchoTerminal && export JAVA_HOME="C:/Program Files/Eclipse
  Adoptium/jdk-17.0.20.101-hotspot" && ./gradlew assembleDebug --no-daemon`.
- On-device matrix (wireless adb): idle + short press → chat starts; during chat +
  short press → chat ends, screen stays on; hold 1 s → muted (wake dead, chip shown,
  LED on if node found), chat if active ends; hold again → recovers; mute survives
  reboot; **screen tap while idle does NOT start a chat**; voice "hey jarvis" still
  starts one; "Jarvis, go mute" flips the same state.
- Report ~15 lines + commit SHAs; state explicitly what the LED spike found.
