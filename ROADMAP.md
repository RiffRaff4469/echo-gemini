# echo-gemini — Roadmap & Status

_Living document. Last updated 2026-09-07 ~02:50 (commit `87b3f78` — spotify backoff + govee brief 13)._
For the day-by-day record of where the project was left, see
[docs/STATUS-2026-09-07.md](docs/STATUS-2026-09-07.md).

---

## TL;DR

| Layer | State |
|---|---|
| Hardware | Echo Show 5 (gen 1, `checkers`) rooted on **custom LineageOS 18.1** (dirty-flash, no-wipe) |
| Device app | **EchoTerminal v1.4** installed (`com.echogemini.terminal.debug`) — ambient WebView clock + voice + camera + alarms/timers |
| Server | Python `echo-server` on the Windows PC, **174+ pytest green**, protocol **v1.5** |
| Network | Device on Tailscale (`echo-show-5-1`), one outbound WebSocket to the PC — no inbound ports |
| Voice | Wake word "hey Jarvis" + **mic-button talk toggle** (brief 7 v2); **screen = UI only** (no tap-to-talk); Jarvis answers in one short sentence |
| Vision | Camera code live; **kernel layer verified healthy** (sensor found, info delivered) — HAL reports 0 devices; wall documented at HAL userspace |
| Music | Spotify (librespot → PCM down the device link): **account linked + binary built + poll backoff fixed — first audio test pending** |
| Lights | **Govee BLE (brief 13): H617A + H617C verified on hardware, voice tool implemented (472 tests) — voice test pending** |

Everything below a checkmark is **done and verified**; un-checked items are
open work. Checkmarks are the only thing that moves an item up the list.

---

## Shipped milestones

- [x] **Phase 1 — Unlock & stock LineageOS** (2026-09-03→06): amonet 2.x exploit,
      rooted LOS 18.1 v0.6, hash-verified TWRP backup. Exploit window preserved
      (never joined Wi-Fi pre-flash). Runbook: `docs/FLASHING.md`.
- [x] **Phase 3 baseline (partial)**: Tailscale on-device (sideloaded APK, no Play
      Services, always-on VPN survives reboots); wireless adb; mic measured —
      near-field speech −45.6 dBFS / 18 dB SNR → **+24–28 dB software gain
      mandated** (`docs/HARDWARE-STATUS.md`).
- [x] **Server core**: WebSocket hub + wake word + Gemini Live (audio & vision)
      + display API + weather. E2E LIVE-verified with a real key (multi-turn
      audio intact). Turn-end defect fixed (`4518e1c`): server sends explicit
      turn-end after 1.2 s silence.
- [x] **Client v1**: ambient clock (zero network dependency), voice, camera,
      WebView ambient layer with sandboxed push cards (UI brief 9).
- [x] **Client v2 — alarms & timers on-device** (`ad4d2e8`): AlarmScheduler /
      AlarmService / Schedule screens, exact-alarm permission, boot receiver.
      APK installed on the Show; hardware alarm-audio test still outstanding.
- [x] **Chat semantics v1.4** (`d67b39e`): tap starts a chat, tap again ends it
      (mid-answer included). Legacy `stay` machinery deleted.
- [x] **Jarvis identity & speech rules** (`c3533b7`): wake "hey jarvis", voice
      Fenrir, answers in ONE short sentence (prompt-driven, no code change to retune).
- [x] **Phase 2 — custom ROM BUILT** (2026-09-06): `lineage-18.1-20260906-UNOFFICIAL-
      checkers.zip` — camera (OV9734) + Bluetooth patches + WebView re-added.
      **Dirty-flashed no-wipe over v0.6 — device boots to launcher, Touchscreen
      expected fine (configs byte-identical to v0.6).**
- [x] **Spotify (brief 12) — code complete** (`de622f5`): librespot supervisor +
      resample + `AUDIO_MUSIC` channel + voice tools + now-playing cards,
      arbitration (music pauses for conversation). Covered by tests against fakes.
- [x] **Memory (brief 8) — code complete** (`0761a85`): Jarvis long-term memory —
      JSON store + remember tool + prompt injection (482 tests green).

## Verify queue — what's open RIGHT NOW

Owner / next-session checklist before more features land:

- [x] **Touch check on custom ROM** — FIXED 2026-09-07 (kernel Y-mirror + GTP_CHANGE_X2Y; X max 960 / Y max 480 verified; owner-confirmed working).
- [x] **Wi-Fi stability** — FIXED 2026-09-07 (duplicate OWE network crashing the supplicant; forgotten; owner-confirmed stable).
- [ ] **Camera on custom ROM** — kernel layer EXONERATED (sensor alive, both cameras' info delivered to the HAL). HAL still reports 0 devices — next step: capture logcat -b all during a boot-time enumeration (only boots trigger the search), look for camera.mt8163/libcam errors after the GETINFO2s. Remaining leads in task log `echo-show-phase2-rom`. Owner deadline: give up if the next session can't crack it.
- [ ] **Bluetooth on custom ROM** — pair a device; confirm audio path.
- [ ] **Mic far-field take** — record at room distance, tune wake threshold + set `MIC_GAIN` to the mandated +24–28 dB in `.env`.
- [ ] **Alarm audio on-device** — set a timer/alarm on the Show, confirm it rings.
- [ ] **Spotify end-to-end** — librespot 0.8.0 built from source + account linked (`spotify=Jarvis`); say "play some lofi" → audio on the Show (see `docs/SPOTIFY.md`).
- [ ] **Govee lights end-to-end** — code done + 37 tests; say "turn on jaiden lights" (H617A) / "patrick lights" (H617C) / "the lights" (both). Strips must be on + Govee phone app closed.

## Shipped 2026-09-07 (late-night run, commit `87b3f78`)

- [x] Touch Y-flip kernel fix flashed + confirmed.
- [x] Wi-Fi OWE crash fix (root cause: duplicate OWE network).
- [x] Spotify: account linked, librespot built (4m43s from source), now-playing poller 429-backoff fix (Claude Code) — 435 tests green, backoff observed live.
- [x] Govee brief 13: H617A + H617C BLE protocol verified on hardware (power + whole-strip color, 0x33/XOR frames on the 2b11 char), `server/govee.py` + `govee_control` voice tool (Codex), 472 tests green, committed + pushed.

## Next up (feature queue — one worker at a time)

Briefs live in `docs/` with git history; a single worker implements one brief
before the next is dispatched (see `docs/ops-playbook.md` in the operator skill).

1. **Live verify round** (owner, ~15 min): Spotify audio, Govee voice, alarm audio, mic far-field.
2. **Camera final push** (parallel ROM lane) — HAL-userspace log capture at boot; then the give-up call.
3. **UI overhaul — owner-approved 2026-09-07** (Nest-Hub style; briefs ordered by
   dependency, one worker at a time):
   1. **HARDWARE-BRIEF-7 v2 — input model change** (`docs/HARDWARE-BRIEF-7-PHYSICAL-CONTROLS.md`):
      mic button short-press = talk toggle, hold ≥ 1 s = mute + LED, **screen taps
      stop starting/stopping chats** (v1.4 tap semantics retired; wake word stays).
   2. **CLOCK-BRIEF — stopwatch + clock pages** (`docs/CLOCK-BRIEF-STOPWATCH.md`):
      alarms/timers/stopwatch all UI + Gemini controllable, on-device, PC-off.
   3. **UI-BRIEF-14 — layout engine + 65/35 home** (`docs/UI-BRIEF-14-LAYOUT-ENGINE.md`):
      focus-driven layout (home/music/chat), widget rail, screen = UI only.
   4. **UI-BRIEF-15 — music focus** (`docs/UI-BRIEF-15-MUSIC-FOCUS.md`): music hero +
      compact clock when playing/paused (needs Spotify e2e first).
   5. **UI-BRIEF-16 — visual answers** (`docs/UI-BRIEF-16-VISUAL-ANSWERS.md`):
      tappable options/lists (trivia), `select` protocol, prompt rules.
4. **UI brief 10 — live data feeds** — re-sequenced AFTER the layout engine (it
   grows the widget gallery; widgets v1 use already-pushed data).
5. **Mic far-field viability decision** — gates wake-threshold tuning; the mic
   button now covers the manual-input fallback (tap-to-talk is gone by design).

## On hold (owner decisions)
- **PS5 / Bluetooth speaker out** — parked by owner 2026-09-06 ("leave the ps5
  speaker idea for now"); revisit after the verify queue clears.
- **Govee cloud API** (`GOVEE_API_KEY` in `.env`) — kept for future Wi-Fi Govee
  devices; strips are BLE-only so BLE is the path.

## Done / retired lanes

- [x] Wake word "Elliot" brief — superseded by **Jarvis** (owner rename).
- [x] Canvas-only ambient UI — superseded by WebView ambient (Canvas kept as
      forced-throw fallback only).

---

## How to know where the project is at

1. **This file** — the roadmap. Start here.
2. **`docs/STATUS-*.md`** — dated stopping-point snapshots (what was left mid-flight).
3. **`git log --oneline`** — the briefs and their outcomes are in the commit
   messages; docs commits describe the brief, code commits land it.
4. **`docs/HARDWARE-STATUS.md`** — measured hardware facts (mic, RAM, camera).

## Working rules (do not regress)

- **The clock never depends on the network.** It must keep working with the PC off.
- **Never force-kill `cameraserver`** — IOMMU livelock, physical power-cycle only.
- **ROM reinstalls silently kill the camera** until the cmdq-event shim is re-run
  (README warning box; HANDOFF §5.7 step 3).
- **The API key never leaves the PC.** `.env` is gitignored; nothing secret goes
  in a commit, a brief, or a chat.
- **Port 8765 is Orbit prod on this PC** — echo-server runs on `ECHO_PORT=8766`.
- **One worker per repo at a time**; commit leftovers after a worker's
  done-notification (`git status` first, always).
