# GOVEE BRIEF 13 — voice-controlled lights via Jarvis

**Status:** protocol VERIFIED on hardware (2026-09-07 ~02:15). H617A + H617C both confirmed working (power + whole-strip color). Implementation dispatched to Claude Code.

## Goal
Jarvis controls Jaiden's Govee LED strips by voice ("hey Jarvis, turn the lights blue"). Both strips respond together.

## Devices
| Strip | BLE name | Address | Voice name | Status |
|---|---|---|---|---|
| H617A RGBIC | `Govee_H617A_0735` | `D2:21:C2:46:07:35` | **jaiden lights** | ✅ VERIFIED (power + whole-strip color) |
| H617C RGBIC | `Govee_H617C_7840` | `D0:05:C1:C6:78:40` | **patrick lights** | ✅ VERIFIED — same protocol as H617A |

"the lights" (no name) = both strips together.

## Verified BLE protocol (H617A, 2026-09-07)
- **Radio**: PC Bluetooth (RZ616); bleak on Windows. Enable radio first if off (PowerShell WinRT).
- **Connect**: scan for the BLE name; device advertises intermittently — retry scans up to ~4×6s. One connection at a time: the Govee phone app must be closed.
- **Write char**: `00010203-0405-0607-0809-0a0b0c0d2b11` (NOT ff01 — ff01 is the old channel; the H617A ACKs there but ignores commands)
- **Notify char**: `00010203-0405-0607-0809-0a0b0c0d2b10`
- **Frame**: 20 bytes = `0x33` + 18-byte body + XOR checksum of bytes 0–18.
- **Power on**: body `01 01` → `33 01 01 00…00 XOR` (off: `01 00`)
- **Whole-strip static color**: body `05 15 01 RR GG BB 00 00 00 00 00 FF 7F` → `33 05 15 01 RR GG BB 00 00 00 00 00 FF 7F 00 00 00 00 00 XOR` (mask 0x7FFF = all 15 segments, little-endian FF 7F)
- Kelvin temp: same body with kelvin u2be in place of direct RGB (0x0000 direct + kelvin set; see ha-govee-led-ble grammar for the exact variant)
- Reference implementation: github.com/teh-hippo/ha-govee-led-ble (`tools/ble/kaitai/command_write.ksy` = the grammar; note their package needs kaitai codegen + Py3.12 — don't import it directly, implement the 5 lines above).

## Architecture (server-side, same pattern as Spotify)
- `server/govee.py`: async module — `GoveeHub` managing N devices (config `GOVEE_DEVICES` = `name:addr,...`), connect on demand with retry, `set_power`, `set_color(rgb)`, `set_brightness`, broadcast to ALL devices for group commands.
- Voice tool registration in `server/main.py` tools: `govee_control` with intents from the model ("turn the lights red" → color; "lights off" → power).
- Optional: a display card like now-playing (nice-to-have, skip for v1).
- Tests in `server/tests/test_govee.py` (packet builder unit tests with the golden bytes above; no hardware needed).

## Jaiden's lane
- API key (cloud, for future wifi devices) already in `.env` as `GOVEE_API_KEY`. Not needed for BLE control.
- Keep the strips powered; close the Govee app when Jarvis should control them (one BLE connection at a time).

## Key files
- `C:/Users/jaide/tools/echo-show-ref/build/govee-color-test2.py` (working test — power + color cycle)
- docs/GOVEE-BRIEF-13.md (this file)
