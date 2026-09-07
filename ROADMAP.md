# echo-gemini — Roadmap & Status

_Living document. Last updated 2026-09-07 (stopping point: commit `de622f5`)._
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
| Voice | Wake word "hey Jarvis" (server-side openWakeWord), tap-to-talk, tap-to-stop; Jarvis answers in one short sentence |
| Vision | Camera code live; **on-device verify pending on the custom ROM** (see Verify queue) |
| Music | Spotify (librespot → PCM down the device link): **code complete + tested, end-to-end pending** |

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

## Verify queue — what's open RIGHT NOW

Owner / next-session checklist before more features land:

- [ ] **Touch check on custom ROM** — axes should be correct (kernel configs and
      `.idc` files are byte-identical to v0.6; if not, rollback is a 15-min dirty
      flash of the v0.6 zip in `tools/echo-show-ref/stage/`).
- [ ] **Camera on custom ROM** — "Hey Jarvis, what do you see?" must return a
      description. (Dead on stock LOS; this is what the Phase-2 build fixes.)
- [ ] **Bluetooth on custom ROM** — pair a device; confirm audio path.
- [ ] **Mic far-field take** — record at room distance, tune wake threshold +
      set `MIC_GAIN` to the mandated +24–28 dB in `.env`.
- [ ] **Alarm audio on-device** — set a timer/alarm on the Show, confirm it rings.
- [ ] **Spotify end-to-end** — fetch librespot binary, one-time browser login,
      "play some lofi" → audio on the Show (see `docs/SPOTIFY.md`).

## Next up (feature queue — one worker at a time)

Briefs live in `docs/` with git history; a single worker implements one brief
before the next is dispatched (see `docs/ops-playbook.md` in the operator skill).

1. **Finish Spotify brief 12** — close the verify queue items above (binary,
   login, first audio).
2. **UI brief 10 — live data feeds** (scores / flights / news / stocks on the
   ambient display; greyed until data).
3. **HARDWARE brief 7 — physical buttons** (mic button remap: end chat / privacy
   deaf; camera-switch verify).
4. **MEMORY brief 8 — Jarvis memory** (persistent context across sessions).
5. **Mic far-field viability decision** — result of the far-field take gates
   wake-word design; if it fails, tap-to-talk is the primary input (already the
   design fallback).

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
