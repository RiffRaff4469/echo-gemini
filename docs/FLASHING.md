# FLASHING.md — Phase 1: unlock → stock LineageOS on Echo Show 5 (gen 1, `checkers`)

> Owned by Hermes (orchestrator). Source of truth for the physical bring-up.
> Synthesized from: `HANDOFF.md` §4, `dallanwagz/echo-show-jailbreak` (verified working,
> checked 2026-09-03), the amonet `mt8163-checkers` branch (cloned, exploit chain dated
> 2026-08-10 = the 2.x generation), and `amazon-oss/releases` (ROM v0.6, checked live).

## 0. Go/no-go gates — all four MUST pass before the exploit runs

| # | Gate | How | Pass = |
|---|---|---|---|
| G1 | Firmware is exactly **6.5.7.3** (or 6.5.7.1) | Settings → Device Options → About | version matches |
| G2 | Wi-Fi is **OFF** and stays off until Phase 1 completes | toggle in settings | no OTA window |
| G3 | Data-capable micro-USB cable | `adb devices` shows the device from the host | serial appears |
| G4 | Barrel-jack 15 W PSU powers the device (micro-USB is data-only, will NOT power it) | physical | boots/charges |

Firmware 6.5.7.3 is one of only two versions the bootrom exploit supports. An OTA patches
the LK bootloader and permanently closes the window. **If the device ever connects to Wi-Fi
before step 7, stop and re-check G1.**

## 1. What you need (physical)

- [ ] Echo Show 5 gen 1 (`checkers`), Fire OS 6.5.7.3, Wi-Fi off
- [ ] **Data-capable micro-USB cable** — charge-only cables are the #1 cause of "exploit doesn't work"
- [ ] Original **barrel-jack 15 W PSU**
- [ ] Windows PC (this one) + a **USB stick ≥ 16 GB** for the Ubuntu live session
      — **or** a Mac as the flash host (supported, see §3.1 — this keeps an agent online the whole session)
- [ ] ~15 GB free on the USB stick for staged downloads (or fetch in the live session — it has network)

## 2. Downloads (stage before you boot Ubuntu)

| File | Where | Notes |
|---|---|---|
| Ubuntu Desktop LTS ISO (24.04) | ubuntu.com/download/desktop | live USB maker |
| `amonet-checkers.zip` | XDA thread (below), OP attachments | **must be the 2.x release** — ships TWRP + `fastbrick.sh`; do NOT use any pre-2026 archive |
| `boot-root.img` | same XDA thread OP | **filename identical across Echo Show models, contents differ — checkers only. Wrong file = bootloop** |
| `lineage-18.1-20260624-UNOFFICIAL-checkers.zip` | `gh release download -R amazon-oss/releases lineage-18.1-checkers-v0.6` (477 MB) | stock unofficial LOS 18.1, v0.6 = 2026-06-24, `.sha256sum` published alongside |
| Android platform-tools | developer.android.com/tools/releases/platform-tools (Linux zip for the live session; macOS: `brew install --cask android-platform-tools`) | adb + fastboot |
| Magisk (official only) | github.com/topjohnwu/Magisk/releases | stub APK + `app-debug.apk` of the SAME release |

XDA threads (R0rt1z2):
- Unlock/root/TWRP/unbrick: `xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-show-5-1st-gen-2019-checkers.4762900`
- LOS 18.1 thread: `xdaforums.com/t/rom-unofficial-11-checkers-lineageos-18-1-for-the-amazon-echo-show-5-2019.4763475`

**Version check at download time:** the amonet release zip should be version 2.0.1+.
1.x is a different exploit generation (microloader payload, old partition layout) — the
camera patches and this runbook do NOT work with it, and mixing versions bricks cleanly.
The GitHub source (`R0rt1z2/amonet`, branch `mt8163-checkers`, "Prepare for new exploit"
2026-08-10) is the same code as the 2.x zip if you prefer to build it yourself.

## 3. Host prep — Ubuntu live session

The exploit MUST run from Linux (or macOS), NOT WSL2: the device re-enumerates as
different USB identities across BROM → preloader → fastboot, and WSL2's usbipd is fragile
in exactly that window. We boot Ubuntu on this PC.

1. Create the live USB (Rufus or balenaEtcher; persistent storage optional but handy).
2. Boot it on the PC (F12/boot menu). Do NOT install — "Try Ubuntu".
3. In the session:
   ```bash
   sudo apt update && sudo apt install -y android-sdk-platform-tools python3-pip python3-usb
   sudo systemctl stop ModemManager   # amonet refuses to run with it active (hard check in main.py)
   sudo systemctl disable ModemManager
   pip install --user pyusb
   ```
4. Verify the cable now, on Ubuntu:
   ```bash
   adb devices        # G3 gate — if empty here, the exploit will fail; swap cable/port
   ```

### 3.1 macOS host (alternative — agent-friendly, recommended if you flash from the Mac)

The Mac is a legitimate flash host: amonet's serial layer supports darwin
(`/dev/cu.usbmodem*`). Two gotchas, both handled below:

1. **The amonet GitHub source checks `/proc` for ModemManager unconditionally** (Linux-only).
   On macOS it crashes with `FileNotFoundError` before doing anything. Patch after cloning —
   in `modules/main.py`, wrap the call (it sits at the top of `main()`):
   ```python
   if os.path.isdir("/proc"):
       check_modemmanager()
   ```
   If you are using the XDA release zip and `main.py` runs unpatchable/fine, skip this.
2. **Documented USB-timing stalls on macOS** during the BROM window. If a step stalls,
   re-run it; if it recurs, swap cable or port. Annoying but not fatal.

Prep:
```bash
brew install --cask android-platform-tools   # adb + fastboot, no driver install needed
brew install python@3.12 coreutils
pip3 install --user pyusb
adb devices        # G3 gate — the cable must enumerate HERE before the exploit
```
No ModemManager on macOS — nothing to stop/disable. macOS may pop an "allow accessory"
permission prompt when the Show attaches — always allow it.

## 4. Boot modes (memorize this table)

| Buttons held at power-on | Mode |
|---|---|
| Volume Down + Volume Up + Mute | **Fastboot** (pre-exploit entry) |
| Mute only | **Hacked Fastboot** (post-exploit) |
| Volume Up only | **TWRP / Recovery** |
| All three | **Stock Fastboot** |

## 5. Unlock (amonet)

```bash
cd amonet-checkers   # unzipped
./fastbrick.sh
```
Device off → hold **Vol Down + Vol Up + Mute**, apply power, keep holding until the
fastboot screen appears → connect USB. The script polls, confirms the device is
`CHECKERS`, and prompts for `YES`.

- **Stall >10 min in the retry loop?** Known USB-timing issue. Try a different cable and a
  different port (rear USB on a desktop). On Linux it usually lands first try.
- Do not touch the device mid-run. Watch for the reboot dance.

## 6. Root boot (still on stock Fire OS)

```bash
# Mute held at power-on → Hacked Fastboot, then:
fastboot oem flags 61
fastboot flash boot boot-root.img    # checkers file only — verify sha256 against XDA OP first
fastboot reboot
```
Device boots Fire OS. Enable Developer Options + USB debugging, then:
```bash
adb shell settings put global disable_bouncer 1
```

## 7. Full pristine backup — NON-NEGOTIABLE, before anything else

This is the only clean route back to working Fire OS (Alexa dies permanently on LOS).
**An untested backup is not a backup.**

Boot TWRP (Volume Up held). These devices are **system-as-root**; TWRP ships a CLI that
replaces the tap-through UI:
```bash
adb shell ls /dev/block/by-name          # confirm map — Show 5's differs from Show 8's
adb shell twrp backup S /sdcard/twrp-backup   # system image at minimum
adb pull /sdcard/twrp-backup ./checkers-backup-<date>/
# or the stronger option — whole-disk dd + boot0/boot1 (see jailbreak notes BACKUP-DD-vs-TWRP):
adb shell dd if=/dev/block/mmcblk0 of=/sdcard/full-dd.img bs=4M
adb pull /sdcard/full-dd.img ./checkers-backup-<date>/   # then verify checksums ON THE HOST
```
**Verify the backup is readable on the host before continuing.** Never restore a pre-2.x
backup onto a 2.x device (partition layout differs).

## 8. Flash stock LineageOS 18.1 (v0.6) — Phase-1 checkpoint build

This is the plain unofficial LOS (no camera patches) — it validates the whole
exploit/flash chain and gives a working device BEFORE the 4–8 h custom build in Phase 2.

```bash
# TWRP session:
adb push lineage-18.1-20260624-UNOFFICIAL-checkers.zip /sdcard/
adb shell sha256sum /sdcard/lineage-18.1-20260624-UNOFFICIAL-checkers.zip   # verify vs .sha256sum asset
adb shell twrp wipe system
adb shell twrp wipe data
adb shell twrp wipe cache
adb shell twrp install /sdcard/lineage-18.1-20260624-UNOFFICIAL-checkers.zip
adb shell twrp reboot
```
**Skip GApps entirely** (design decision — 1 GB RAM, nothing needs Play Services).
First boot takes a while. Expect a tablet-ish launcher, not Fire OS.

## 9. Kiosk baseline (repeat after any flash — settings have been observed reverting)

```bash
adb shell locksettings set-disabled true
adb shell settings put secure screensaver_enabled 0
adb shell settings put secure screensaver_activate_on_sleep 0
adb shell settings put secure screensaver_activate_on_dock 0
adb shell settings put secure doze_enabled 0
adb shell settings put secure doze_always_on 0
adb shell settings put secure doze_pulse_on_pick_up 0
adb shell settings put secure doze_pulse_on_double_tap 0
adb shell settings put system screen_off_timeout 1800000
```

## 10. Magisk root (official releases only, checksum-verify)

Two assets from the SAME release:
1. `Magisk-vXX.X.apk` (stub) → rename `.zip`, push, `twrp install`. Patches `boot`.
2. On first boot the stub offers "upgrade to full Magisk" — **decline**; instead
   `adb install -r app-debug.apk` from the same release.

Pitfalls:
- **"Requires additional setup… Recovery mode cannot get correct device info"** → root
  exists but the TWRP-mode patch is incomplete. Fix from the running OS: Magisk app →
  Install → **Direct install (Recommended)** → reboot.
- **`su` returns Permission denied with no popup** → Magisk Superuser tab may hold a
  `[SharedUID] Shell` entry toggled OFF (silently auto-denied). Toggle it ON.

## 11. Verify the unlock generation + baseline

```bash
adb -s <serial> shell 'dd if=/dev/block/by-name/recovery bs=512 count=2 2>/dev/null' \
  | grep -qa microloader && echo "amonet 1.x - UPGRADE REQUIRED" || echo "amonet 2.x - ok"

adb -s <serial> shell getprop ro.product.device        # expect: checkers
adb -s <serial> shell getprop ro.build.version.release # expect: 11
adb -s <serial> root && adb -s <serial> shell id       # expect: uid=0(root)
adb -s <serial> shell cat /proc/meminfo | head -1      # RECORD ACTUAL RAM → docs/HARDWARE-STATUS.md
```

## 12. Handoff to Phase 3 (HANDOFF §6 checklist)

- [ ] Touchscreen, Wi-Fi, speaker, light sensor respond
- [ ] Bluetooth — NOTE: broken on stock LOS 18.1; fixed only by the Phase 2 camera/bt build (patch 0017). Do not chase it here.
- [ ] Camera — NOT functional on stock LOS (expected; Phase 2 fixes it). Do not chase it here.
- [ ] **Mic measurement** — record 30 s at speaking distance via `adb`, pull WAV, inspect levels → writes the gain constant + near/far-field verdict into `HARDWARE-STATUS.md`. This is the project's central risk gate.
- [ ] Tailscale APK (armeabi-v7a) sideload — see HANDOFF §6

## 13. Unbrick paths

- **Recovery does not depend on a working bootloader** — the entry is a BootROM/Preloader
  exploit. amonet 2.x ships **Preloader USBDL recovery mode** (the documented unbrick path
  in the XDA thread).
- Hang at vendor logo → an amonet 1.x boot image was flashed. Reflash the plain `boot.img`
  via fastboot or TWRP.
- Truly unrecoverable: physical damage, or an OTA taken before unlock.

## 14. Pitfalls table

| Symptom | Cause / fix |
|---|---|
| Script says ModemManager | `sudo systemctl stop ModemManager` (hard exit in amonet main.py) |
| Exploit stalls in retry loop | USB timing — new cable / different port; never WSL2 |
| `adb devices` empty | charge-only cable — G3 gate exists for this |
| Bootloop after boot-root | wrong boot-root.img (cronos package or wrong gen) |
| Backup "restored" and device dead | pre-2.x backup onto 2.x device — partition layout differs |
| `su` denied | Magisk SharedUID Shell entry toggled off, or need Direct-install re-patch (§10) |
