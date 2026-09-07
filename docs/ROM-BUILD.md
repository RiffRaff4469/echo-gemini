# Phase 2 — custom LineageOS ROM build (camera + Bluetooth)

_Concise map. The full Phase-1 flash runbook is [FLASHING.md](FLASHING.md);
measurements log is [HARDWARE-STATUS.md](HARDWARE-STATUS.md)._

## Why a custom build

Stock LineageOS 18.1 (v0.6) on `checkers` (Echo Show 5 gen 1) has a **dead
camera** (OV9734) and no working Bluetooth. The Phase-2 build adds the
community camera patches and BT fixes at the source level, then rebuilds.

## Upstream

- Camera + BT patches: **jxlarrea/lineageos-echo-show-camera**
  (community-verified for `checkers`; needs the pinned `libdpframework` build
  the repo's install scripts select).
- For `checkers`: patches **0001, 0014, 0015, 0016** (kernel/sensor) + ROM
  patches **0005/0006/0011–0013/0017**. Skip the OV02B10 steps — those are for
  `cronos` (Show 8), not this device.

## Build host

- **WSL2, Ubuntu 22.04** on an external NTFS SSD (the T7, D:) — ~873 GB free.
  The C: drive is too small (~86 GB free) for a full tree.
- `.wslconfig` gives the distro 12 GB RAM / 6 CPUs; ccache capped at 8 GB.
- Bootstrap is scripted: `tools/echo-show-ref/build/rom-build-bootstrap.sh`
  (stages: deps → tree → sync → patches → build). Build itself is `mka bacon`.
- WSL2 is fine for building but NOT for the amonet exploit (USB re-enumeration);
  the flash path is TWRP + adb from Windows.

## Current artifact (as of 2026-09-06)

- `lineage-18.1-20260906-UNOFFICIAL-checkers.zip` (~484 MB) — camera blobs +
  BT patches + **WebView re-added** (the pdk/LFS re-add was required; without
  it the ROM has no WebView for the ambient UI).
- Boots as `eng.builde.20260906`.

## Flash technique — DIRTY FLASH IS THE DEFAULT

Both v0.6 and the custom build are `eng.*userdebug/test-keys` builds of the
same tree, so swapping between them needs **no wipe**:

```
TWRP → twrp install <zip> → reboot
```

No wipe preserves /data: provisioned flags (no wizard), saved Wi-Fi, Tailscale
login, installed APKs, permissions, screensaver-off. Dirty flash is used for
v0.6 → custom and custom → v0.6 rollback (~15 min each way).

Known post-flash re-does (device-specific, not wipe-related):

- Re-join Wi-Fi via `cmd wifi connect-network <ssid> open` — a saved network
  does NOT auto-join across a ROM swap (OWE transition quirk).
- One manual Tailscale launch (force-stop/always-on suppression on fresh boot),
  then the tunnel sticks.
- Re-assert screensaver-off.

## Pitfalls (each cost real time)

1. **ROM reinstall silently kills the camera** until the cmdq-event shim is
   re-run: `shims/libcmdqevent/build.sh` → `scripts/install-cmdq-event-shim.sh`
   → reboot. Symptom: `Number of camera devices: 0`, everything else normal.
   Record every reinstall in HARDWARE-STATUS.md §5.
2. **Touch regression scare (2026-09-06)**: a custom kernel briefly booted with
   swapped touch axes. Root cause was a kernel dts difference, NOT the idc
   `touch.orientation` value (InputReader largely ignores it). Fix pattern:
   `touchscreen-swapped-x-y;` in the Goodix node of `checkers.dtsi` → `mka
   bootimage` (~15 min) → flash ONLY boot.img. Current custom build's configs
   and idc files are byte-identical to v0.6 — do not re-hunt kernel defconfigs
   if touch looks wrong; check the boot.img kernel hash first.
3. **Never force-kill/restart `cameraserver` while streaming** → IOMMU livelock;
   physical power cycle only.
4. Do not pipe `breakfast`/`lunch` in WSL (subshell kills exports) — write
   helper scripts and run them via `wsl.exe -d Ubuntu-22.04 -- bash <script>`.

## Verify after a flash

1. Touch: tap around the launcher.
2. Camera: "Hey Jarvis, what do you see" (or `adb shell` camera HAL check).
3. Bluetooth: pair a device.
4. Log the result in `docs/HARDWARE-STATUS.md`.
