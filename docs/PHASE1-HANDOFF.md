# PHASE-1 HANDOFF — Echo Show 5 (gen 1, `checkers`) unlock → rooted LineageOS 18.1

> **To:** Hermes (orchestrator) and any agent picking up Phase 2 or Phase 3.
> **From:** Phase-1 flashing copilot session, 2026-09-04 → 2026-09-06.
> **Scope executed:** stock Fire OS → amonet 2.0.1 unlock → TWRP → verified full backup →
> stock unofficial LineageOS 18.1 v0.6 → Magisk v30.7 → smoke test → STOP.
> Phase 2 (custom camera/BT ROM) and Phase 3 (mic measurement, Tailscale) were NOT started.

## 1. Bottom line

**Phase 1 completed successfully.** The device is a working, rooted LineageOS 18.1 unit.
No bricks, no bootloops, no OTA. The exploit window was never exposed. A full, verified,
host-side backup of the pristine Fire OS state exists and is the only route back.

Two failure modes in this phase were caused by *incorrect guidance in `docs/FLASHING.md`*,
not by the hardware. Both are documented in §6 below and should be folded back into the
runbook before anyone repeats this. One of them (§6.2) would have produced a silently
corrupt backup that looked fine.

## 2. Final device state

| Property | Value |
|---|---|
| Model | Echo Show 5, 1st gen (2019), `H23K37` |
| Codename | `checkers` (confirmed by amonet, by ROM metadata, and by `ro.product.device`) |
| Serial | `G0913L0594031A7Q` |
| Firmware before | Fire OS **6.5.7.3**, build `NS6573/7595` |
| Bootloader | Unlocked via amonet **2.0.1**; `fos_flags` set to `3d` (61 decimal) |
| LK build (pre-exploit) | `44072a3-20240709_170103` |
| amonet payload used | `fastbrick-20240709.img` |
| amonet generation | **2.x — confirmed twice** (see §4) |
| Recovery | TWRP (installed by amonet), functional, CLI-driven |
| OS now | LineageOS 18.1 `lineage_checkers-userdebug 11 RQ3A.211001.001 eng.r0rt1z.20260624.024750 test-keys` |
| Android release | `11` (SDK 30), security patch `2024-02-05` |
| Root | Magisk **v30.7** (`30.7:MAGISK:R`, versionCode 30700, pkg `com.topjohnwu.magisk`), `su` at `/system/xbin/su` |
| GApps | **None**, deliberately (hard rule 7) |
| **RAM (MemTotal)** | **996,988 kB ≈ 974 MiB** |
| Alexa | **Gone permanently.** Only the §3 backup restores it. |

### Working
Display, touchscreen, speakers, Wi-Fi, volume + mute buttons, light sensor —
all operator-confirmed 2026-09-06.

### Expected-broken (do NOT debug — Phase 2 fixes these)
- **Camera.** `pm list features` declares `android.hardware.camera` and
  `android.hardware.camera.any` but **not** `android.hardware.camera.front`.
  Declared features are not a working HAL; the real symptom to look for later is
  `Number of camera devices: 0`. Not investigated further, per hard rule 8.
- **Bluetooth.** Not tested, not debugged. Fixed by Phase 2 patch 0017.

## 3. THE BACKUP — protect this

**Location:** `/mnt/t7/checkers-backup-20260905/` on the Samsung T7's exFAT data partition.

| File | Size | Notes |
|---|---|---|
| `mmcblk0-full.img` | 7,820,083,200 B | whole eMMC, sha256 `157de23b34b6f2bc70ba1f84ffd007a06e84debd1dae99e54cc418b4751604df` |
| `mmcblk0boot0.img` / `mmcblk0boot1.img` | 4,194,304 B each | eMMC hardware boot areas — a TWRP backup would NOT capture these |
| 13 individual partition images | ~100 MB total | `kb, dkb, lk_real, tee1_real, logo, tee2_real, expdb, MISC, boot, p10, recovery, persist, metadata` |
| `SHA256SUMS.txt` | — | checksums of all 16 images |

**Verified readable on the host** (hard rule 4), by four independent checks:
GPT parses with 16 partitions whose sizes match the measured map; `boot-p9.img` begins with
`ANDROID!`; partition 12 (system) was loop-mounted read-only and contained a genuine Fire OS
tree (`build.prop`, `framework`, `fake-libs`); all checksums recorded.

> **RISK — act on this.** The backup currently lives only on the T7, which is also the
> Ventoy boot medium for the live session. A reformat or reuse of that drive destroys the
> only path back to working Fire OS. **Copy it to redundant storage.**

> **Never restore a pre-2.x backup onto this device** — the partition layout differs.
> This backup was taken from a 2.x device and is only valid for a 2.x device.

## 4. Why we are confident this is amonet 2.x

Two independent confirmations (this matters because the Phase 2 camera patches cannot run
on 1.x):

1. **The recovery microloader check** (`FLASHING.md` §11) returned `amonet 2.x - ok`.
2. **The partition map signature.** amonet 2.x points `lk`, `tee1` and `tee2` at
   `/dev/null` so they cannot be overwritten, exposing the true partitions as `lk_real`,
   `tee1_real`, `tee2_real`. This was observed directly. **Consequence: reading `lk`
   yields zero bytes — always use `lk_real`.**

## 5. Measured partition map (`checkers`, post-amonet-2.x)

eMMC `mmcblk0` = 7,636,800 × 1K = 7.28 GiB = 7,820,083,200 B.
`mmcblk0boot0`/`boot1`/`rpmb` = 4,096 K each.

| by-name | node | 1K blocks | | by-name | node | 1K blocks |
|---|---|---|---|---|---|---|
| kb | p1 | 1024 | | boot | p9 | 16384 |
| dkb | p2 | 1024 | | *(unnamed)* | p10 | 16384 |
| **lk_real** | p3 | 1024 | | recovery / swdl | p11 | 32768 |
| **tee1_real** | p4 | 5120 | | system | p12 | 3177472 |
| logo | p5 | 1024 | | cache | p13 | 262143 |
| **tee2_real** | p6 | 5120 | | persist | p14 | 16385 |
| expdb | p7 | 16384 | | metadata | p15 | 40448 |
| MISC | p8 | 512 | | userdata | p16 | 4042543 |

`recovery` and `swdl` are the **same partition** (both → p11).
`/sdcard` **is** userdata (p16) — this matters, see §6.3.

## 6. Runbook corrections — fold these into `docs/FLASHING.md`

### 6.1 `lsusb` is the wrong USB-enumeration test (cost a full session)
In fastboot/preloader entry this device **alternates between two USB identities every few
seconds**:

```
0bb4:0c01  "Android"          (MediaTek)
0e8d:2000  "MT65xx PreLoader" (MediaTek) → binds cdc_acm as ttyACM0
```

`lsusb` is a snapshot and misses both windows. The result is indistinguishable from a
charge-only cable — it produced a wrong diagnosis, a halted session, and an unnecessary
cable purchase. **Always watch continuously instead**, started *before* power is applied,
with USB connected first:

```bash
sudo dmesg -wH | grep -i --line-buffered -E 'usb|mediatek|preloader'
```

Related: because `0e8d:2000` binds `cdc_acm`/`ttyACM0`, **ModemManager must be stopped on
every boot** of the live session, not once. It reclaims the port and amonet hard-exits.

### 6.2 `adb exec-out` merges stderr into the data stream (would corrupt backups)
`adb exec-out "dd if=… count=1"` returned **1,048,668** bytes where **1,048,576** was
expected — 92 extra bytes of `dd`'s "records in/out" summary embedded in the image.
`adb exec-out "cat /dev/block/…"` is byte-exact (verified: 524,288 B from p8, exactly).
**Use `cat`, not `dd`, for any image streamed over `exec-out`.**

Also: **TWRP's toybox `dd` rejects suffixed block sizes.** `bs=1M` →
``block size `1M': illegal number``. Use plain bytes (`bs=1048576`) if `dd` is unavoidable.

### 6.3 §8's wipe/push order deletes the ROM it just pushed
`FLASHING.md` §8 pushes the ROM to `/sdcard`, then runs `twrp wipe data`. `/sdcard` *is*
userdata. **Wipe first, then push.** (In practice TWRP reported
`Wiping data without wiping /data/media`, so it may survive — but the documented order
should not depend on that.)

### 6.4 TWRP's own backup cannot be used on this device
`twrp backup S` needs 2356 MB; `/sdcard` had 1795 MB free, because it backs up to the same
storage it is imaging. **Stream to the host instead** (§6.2 method). This is not a
workaround — it is the only viable route, and it captures more (boot0/boot1).

### 6.5 Gate G3 as written is unsatisfiable
`FLASHING.md` §0 defines G3 as "`adb devices` shows a serial". Stock Fire OS on this unit
exposes **no USB-debugging toggle**, so adb can never enumerate pre-exploit. Substitute a
USB-enumeration check per §6.1. Nothing is lost — amonet drives the MediaTek boot ROM, not adb.

### 6.6 Ventoy layout and mounting
- The data partition is **`sda1`** (931.5G exFAT, label `Ventoy`), **not `sdX2`**.
  `sda2` is a 32M ESP.
- `/dev/sda1` **cannot be mounted directly** during a Ventoy live boot — Ventoy's
  device-mapper devices hold it and `mount` returns `EBUSY`
  ("already mounted or mount point busy"). **Mount `/dev/mapper/sda1`.**
- It mounts **root-owned**; writes fail with EPERM. Mount with
  `-o uid=$(id -u),gid=$(id -g),umask=000`.
- `findmnt -S` does not exist in this Ubuntu 24.04 live session
  (`invalid option -- 'S'`) and its failure reads as a false negative. Use `/proc/mounts`.

### 6.7 Mode entry: the Mute-button route did not work
Holding **Mute only** at power-on booted normally rather than entering Hacked Fastboot on
this unit. What worked reliably:

| Target | Method |
|---|---|
| Fastboot (from TWRP) | `adb reboot bootloader` |
| Recovery (from fastboot) | `fastboot reboot recovery` |
| TWRP (physical) | **Volume Up** held at power-on |

Prefer the adb/fastboot routes over button timing.

### 6.8 ROM checksums live in the release *body*, not as assets
For `checkers` v0.5 / v0.6 / v0.7 the sha256 is published in the GitHub release **body
text**; only v0.1–v0.4 ship a `.sha256sum` asset. A direct asset URL 404s. Retrieve with:

```bash
curl -s 'https://api.github.com/repos/amazon-oss/releases/releases?per_page=100' | grep -iE 'checkers'
```

### 6.9 adb permissions change per mode on Linux
LineageOS presents a different USB VID than TWRP, producing
`no permissions (missing udev rules?)`. Fix with a udev rule covering all three modes
(`18d1`, `0e8d`, `0bb4`) or run the adb server as root. Restarting the server regenerates
the adb key, so **the on-device RSA prompt reappears** and must be accepted.

## 7. Verification evidence

```
amonet generation           : amonet 2.x - ok
ro.product.device           : checkers
ro.build.version.release    : 11
ro.build.display.id         : lineage_checkers-userdebug 11 RQ3A.211001.001
                              eng.r0rt1z.20260624.024750 test-keys
adb root                    : uid=0(root) gid=0(root) … context=u:r:su:s0
MemTotal                    : 996988 kB
MemFree / MemAvailable      : 59896 kB / 290016 kB  (first boot, stock, nothing stripped)
Magisk                      : 30.7:MAGISK:R  (30700)
ROM sha256 (host & device)  : 8a0c7f5daffe2b14b8f5e59219d2d8a1c04c534460e87459e2108b8d677db32f
                              — matched the published v0.6 release hash
boot-root.img sha256        : 37b4cc8eb23ed1434e5b71103e5e8b206d532ed1e0d51bf808dcdd8a99ea5057
                              — magic ANDROID!, 8,189,952 B. NOTE: no reference hash was
                              available offline; XDA cross-check in §6 was NOT performed.
```

## 8. Decisions taken, with rationale

1. **Backup taken BEFORE `boot-root.img`**, reversing `FLASHING.md` §6/§7 ordering.
   Flashing boot-root overwrites the stock boot partition; backing up afterward would have
   captured a modified one. §7's own text says the backup comes "before anything else".
2. **Stayed on ROM v0.6, not v0.7.** A newer release exists —
   `lineage-18.1-20260904-UNOFFICIAL-checkers.zip`, sha256
   `785fa643fd68b2e6f6f02d96a2da58373c6a577b92a27cf6cec69603bb94068e`. v0.6 is what the
   project spec pins and what Phase 2 presumably targets. **Phase 2 should decide
   explicitly whether to move to v0.7 before building camera patches.**
3. **Skipped §6's Fire OS post-boot steps** (enable Developer Options / USB debugging /
   `disable_bouncer`). No USB-debugging toggle exists on this Fire OS build, and the work
   would have been destroyed by the LineageOS flash two steps later.
4. **Wi-Fi ban lifted after the flash.** The prohibition existed solely to prevent a Fire OS
   OTA closing the exploit window. Fire OS is gone; Wi-Fi is now required and safe.

## 9. Open items / NOT done

- **Kiosk baseline (`FLASHING.md` §9) — NOT CONFIRMED APPLIED.** The command block was
  issued but its execution was never verified. `FLASHING.md` notes these settings have been
  observed reverting. **Re-run and verify at the start of the next phase.**
- **Functional Magisk `su` test not run.** `adb root` returning `uid=0` proves nothing here —
  this is a `userdebug` build where `adbd` runs as root regardless. To confirm Magisk grants
  root to *apps*, run `adb shell "su -c id"` and watch for the Magisk prompt. If it returns
  `Permission denied` with no popup, apply the §10 fix: Magisk → Superuser → toggle
  **`[SharedUID] Shell`** ON.
- **`boot-root.img` provenance unverified** — no reference hash was obtainable offline.
- **Bluetooth never tested** (expected-broken; deliberately not investigated).
- **Backup exists on one drive only** — see §3.

## 10. Next steps

- **Phase 3 — mic measurement is the project's central risk gate.** Per `HANDOFF.md` §12,
  the microphone, not the flashing or the ROM build, is the real technical risk. On
  LineageOS only one mic of the array is live and gain is low. Measure before writing any
  app config; the result decides far-field vs. near-field/tap-to-talk design.
  Procedure and the table to fill: `docs/HARDWARE-STATUS.md` §1.1.
- **Phase 3 — Tailscale** (`armeabi-v7a` APK sideload), per `HANDOFF.md` §6.
- **Phase 2 — custom camera/Bluetooth ROM build** (4–8 h). Note `HARDWARE-STATUS.md` §5:
  **reflashing the ROM silently breaks the camera until the shim step is re-run**, with
  `Number of camera devices: 0` as the only symptom.
- **Plan for ~974 MiB of RAM, not 2 GB.** `HANDOFF.md` §2 warns that the camera project's
  "amonet 2.x gives full 2 GB" claim is specific to `crown` (Echo Show 8). It has now been
  measured on this unit and does not apply.

## 11. Host environment (for reproducing)

Ubuntu 24.04 **live** session (not installed), booted via Ventoy from a Samsung T7 on an
Acer Nitro laptop. Everything in `~` is lost on reboot — platform-tools, the amonet
extraction, and the ModemManager change all need redoing each boot. Payloads live at
`/mnt/t7/echo-flash/` (mount `/dev/mapper/sda1`, see §6.6).

Per-boot prep: mount T7 → extract `platform-tools.zip` to `~/pt` and add to PATH →
extract `amonet-checkers-v2.0.1.zip` to `~/amonet` → `systemctl stop/disable ModemManager`
→ confirm `python3 -c "import usb.core"`.
