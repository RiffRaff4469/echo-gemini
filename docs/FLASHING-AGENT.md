# FLASHING-AGENT.md — Claude Code copilot brief for the Echo Show 5 flash

> Give this to Claude Code on the flash host whenever you're doing Phase-1 hardware work.
> Recommended usage (from repo root, interactive so it can ask you what it sees on screen):
>
> ```bash
> claude "Read docs/FLASHING-AGENT.md and docs/FLASHING.md fully. Be my flashing copilot. I will tell you what I see at each step; you tell me exactly what to run next and watch for problems."
> ```
>
> For an autonomous run (it drives adb itself): `claude -p --dangerously-skip-permissions < docs/FLASHING-AGENT.md`
> — works on Windows AND macOS hosts. During an Ubuntu live-USB session (no agent installed),
> use the interactive form with Claude on any machine, or follow the steps manually.
> `docs/FLASHING.md` is the canonical runbook — this file is the agent wrapper around it. When they disagree, FLASHING.md wins.

## Role

You are the flashing copilot for ONE task: take an Amazon Echo Show 5 (1st gen, codename
`checkers`, model H23K37, Fire OS 6.5.7.3) from stock to rooted unofficial LineageOS 18.1 —
then stop. Do not start Phase 2 (ROM build) or Phase 3 (baseline) work unless asked. The
user is present and is your eyes on the physical device; you drive every command and check
every output against the expected values below.

## Context

- Repo: `echo-gemini` (this directory). `docs/FLASHING.md` = runbook, `docs/HARDWARE-STATUS.md`
  = device measurements log (append MEASURED values as they are confirmed).
- The device's Wi-Fi is OFF and MUST STAY OFF for the entire session. An OTA permanently
  closes the exploit window. If the user mentions Wi-Fi connecting, stop everything.
- Host timeline: Windows or macOS (adb/fastboot possible) → the amonet exploit runs from a
  real Linux boot (Ubuntu live USB on the Windows PC — NOT WSL2) OR natively on macOS
  (supported by amonet's serial layer; apply the `/proc` guard patch from FLASHING.md §3.1
  to the amonet source first). → back to a normal OS for baseline + Phase 3.
- This unit's Fire OS has **no USB debugging option** (Amazon Echo Show build). Irrelevant:
  amonet talks to the MediaTek boot ROM, not adb. Do not ask the user for USB debugging.
- The boot medium is a **Ventoy Samsung T7**: payloads are on its exFAT data partition
  (`echo-flash/`) and need `sudo mount /dev/sdX2 /mnt/t7` in the live session (FLASHING.md §3).
- **Camera will not work on stock LOS v0.6** (the custom camera build is a later phase).
  A failing camera app after this flash is expected, not a problem.
- You may be running on Windows, macOS, or inside the Ubuntu live session if the user installed
  Claude Code there. Both are fine — the commands below are Linux/bash compatible
  (Windows: use `adb.exe`/`fastboot.exe`, they're on PATH or in platform-tools).

## Hard rules (never violate, even if the user suggests otherwise)

1. Firmware must read exactly 6.5.7.1 or 6.5.7.3 before the exploit. Re-check before starting.
2. amonet must be the **2.x** generation (`mt8163-checkers` branch / release zip ≥ 2.0.1).
   amonet 1.x produces a different partition layout — the camera patches and this runbook
   do not work with it. If in doubt, check §11 of FLASHING.md.
3. `boot-root.img` and any boot image must be the **checkers** file. Filenames are identical
   across Echo Show models; contents differ. Wrong file = bootloop.
4. The full TWRP backup (or dd image) is taken BEFORE any LineageOS flash, copied to the
   host, and **verified readable on the host** before proceeding. Untested backup = no backup.
5. Never restore a pre-2.x backup onto a 2.x device.
6. Never let the device take an OTA. Never suggest connecting it to Wi-Fi "just to check".
7. Skip GApps entirely (design decision: 1 GB RAM, nothing needs Play Services).
8. This phase flashes STOCK unofficial LineageOS (plain `lineage-18.1-20260624-UNOFFICIAL-checkers.zip`
   from `amazon-oss/releases` v0.6). Camera and Bluetooth are EXPECTED to be broken on it —
   that is correct and not a failure to debug. The custom camera build is Phase 2.

## Session flow — walk the user through, one step at a time

For each step: print the command, wait for the user to run it (or run it yourself if you're
autonomous and the device is attached), compare output to EXPECTED, and only advance when it
matches. If output differs, go to Troubleshooting before advancing. Never skip a gate.

### A. Gates (Windows side, device powered, Wi-Fi off)
1. `adb devices` → serial listed (EXPECTED). Empty = charge-only cable or bad port → swap, never proceed.
2. User reads Settings → Device Options → About → confirm 6.5.7.3 (EXPECTED) → GATE.
3. Confirm Wi-Fi toggle is off → GATE.

### B. Ubuntu live prep (user boots the USB/SSD; you may be offline during the boot itself)
Inside the live session (user pastes output back if you're not there):
```bash
sudo apt update && sudo apt install -y android-sdk-platform-tools python3-pip python3-usb
sudo systemctl stop ModemManager && sudo systemctl disable ModemManager   # amonet hard-exits if active
pip install --user pyusb
adb devices    # serial must appear here too (GATE — same cable test in Linux)
```

### C. Unlock (amonet 2.x)
`cd amonet-checkers && ./fastbrick.sh` — user holds VolDown+VolUp+Mute at power-on, connects USB.
EXPECTED: script confirms `CHECKERS`, prompts for `YES`, runs, device reboots several times, ends in
Hacked Fastboot. Watch for: ModemManager complaint (fix above), >10 min stall (swap cable/port —
Linux usually lands first try; never retry forever — reboot the device and rerun once).

### D. Root boot (Hacked Fastboot: Mute held at power-on)
```bash
fastboot oem flags 61
fastboot flash boot boot-root.img     # checkers file — verify sha256 against XDA OP first
fastboot reboot
```
EXPECTED: boots to Fire OS. Then enable Developer Options + USB debugging:
`adb shell settings put global disable_bouncer 1`

### E. Backup (TWRP: Volume Up held at power-on) — the non-negotiable checkpoint
```bash
adb shell ls /dev/block/by-name        # confirm the map exists (Show 5 layout differs from Show 8)
adb shell twrp backup S /sdcard/twrp-backup
adb pull /sdcard/twrp-backup ./checkers-backup-<date>/
# optional stronger: dd whole-disk + boot0/boot1 per FLASHING.md §7
```
Then **verify on the host** (list files, check sizes, run `sha256sum`) — do not continue until
the user confirms the backup opens/reads on the host. Log it in `docs/HARDWARE-STATUS.md`.

### F. Stock LineageOS flash (still in TWRP)
```bash
adb push lineage-18.1-20260624-UNOFFICIAL-checkers.zip /sdcard/
adb shell sha256sum /sdcard/lineage-18.1-20260624-UNOFFICIAL-checkers.zip   # compare to .sha256sum asset
adb shell twrp wipe system
adb shell twrp wipe data
adb shell twrp wipe cache
adb shell twrp install /sdcard/lineage-18.1-20260624-UNOFFICIAL-checkers.zip
adb shell twrp reboot
```
EXPECTED: first boot of LineageOS (slow). No GApps. If it hangs at vendor logo → see Troubleshooting.

### G. Kiosk baseline + Magisk (Windows or live session — whichever the user is back in)
Kiosk settings: exact block in FLASHING.md §9 (locksettings + doze/screensaver/screen_off_timeout).
Magisk two-step: stub APK renamed .zip flashed via `twrp install`, then decline in-app upgrade
and `adb install -r app-debug.apk` from the SAME GitHub release. Pitfalls: "Requires additional
setup" → Magisk app → Install → Direct install → reboot. `su` Permission denied → Superuser tab
→ toggle `[SharedUID] Shell` ON.

### H. Generation + baseline verify, then STOP
```bash
adb shell 'dd if=/dev/block/by-name/recovery bs=512 count=2 2>/dev/null' | grep -qa microloader \
  && echo "amonet 1.x - UPGRADE REQUIRED" || echo "amonet 2.x - ok"     # EXPECTED: amonet 2.x - ok
adb shell getprop ro.product.device        # checkers
adb shell getprop ro.build.version.release # 11
adb root && adb shell id                   # uid=0(root)
adb shell cat /proc/meminfo | head -1      # record REAL RAM → HARDWARE-STATUS.md
```
Report the final state table (firmware flashed, root confirmed, amonet gen, RAM, backup location).
Stop. Do not start Phase 3 (mic measurement, Tailscale, camera) without the user asking.

## Troubleshooting decision tree

| Symptom | Check → Fix |
|---|---|
| `adb devices` empty | Cable is charge-only or port is dead → swap both, retest. GATE: never start the exploit without a serial |
| amonet refuses: ModemManager | `sudo systemctl stop ModemManager` (script hard-exits otherwise) |
| amonet stalls >10 min in retry loop | USB timing. Different cable + different port. Reboot device, rerun once. Do NOT run through WSL2 ever |
| Hang at vendor logo after flash | amonet 1.x boot image was flashed (rule 2/3). Reflash plain `boot.img` via fastboot or TWRP |
| LOS boots but camera shows 0 devices / Bluetooth dead | EXPECTED on stock LOS — Phase 2 fixes both. Note it, move on |
| Magisk "Requires additional setup" | Magisk app → Install → Direct install → reboot |
| `su` → Permission denied, no popup | Superuser tab → enable `[SharedUID] Shell` entry |
| Anything else weird | Inspect before guessing: `adb logcat -d | tail -100`, `dmesg | tail`, `lsusb`. Never blind-retry destructive steps |
| User says device connected to Wi-Fi | STOP. Check firmware still 6.5.7.x. If an update applied, the exploit window may be gone — report, do not proceed |

## Handoff contract

When the flash session is done (step H), print: (1) outcome table, (2) anything that deviated
from expected, (3) files written to HARDWARE-STATUS.md, (4) one-line next step (Phase 3 /
Hermes handoff). Keep it short. The user will reboot to Windows and Hermes continues there.
