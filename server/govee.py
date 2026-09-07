"""Connect-on-demand BLE control for Govee RGBIC strips (GOVEE-BRIEF-13)."""

from __future__ import annotations

import asyncio
import logging
import re
from functools import reduce
from operator import xor

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakBluetoothNotAvailableError

log = logging.getLogger("echo.govee")
WRITE_CHAR = "00010203-0405-0607-0809-0a0b0c0d2b11"
DEFAULT_DEVICES = "H617A:D2:21:C2:46:07:35,H617C:D0:05:C1:C6:78:40"
ALIASES = {"jaiden lights": "H617A", "patrick lights": "H617C"}


def packet(body: bytes) -> bytes:
    if len(body) > 18:
        raise ValueError("Govee payload exceeds 18 bytes")
    frame = b"\x33" + body.ljust(18, b"\x00")
    return frame + bytes([reduce(xor, frame)])


def _integer(value: int, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"Expected an integer from 0 to {maximum}")
    return value


def power(on: bool) -> bytes:
    if type(on) is not bool:
        raise ValueError("Power must be true or false")
    return packet(bytes([0x01, int(on)]))


def color(red: int, green: int, blue: int) -> bytes:
    rgb = bytes(_integer(v, 255) for v in (red, green, blue))
    return packet(b"\x05\x15\x01" + rgb + bytes(5) + b"\xff\x7f")


def brightness(percent: int) -> bytes:
    return packet(bytes([0x04, _integer(percent, 100)]))


def parse_devices(value: str) -> dict[str, str]:
    devices = {}
    for entry in value.split(","):
        name, separator, address = entry.strip().partition(":")
        name, address = name.strip().upper(), address.strip().upper()
        if (not separator or not name or name in devices
                or not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address)):
            raise ValueError("GOVEE_DEVICES must contain unique name:MAC entries")
        devices[name] = address
    return devices


class GoveeHub:
    def __init__(self, devices: str = DEFAULT_DEVICES) -> None:
        self.devices = parse_devices(devices)
        self._lock = asyncio.Lock()

    def _targets(self, target: str | None) -> dict[str, str]:
        if target is None or target.strip().lower() in ("all", "lights", "the lights"):
            return self.devices
        name = ALIASES.get(target.strip().lower(), target.strip().upper())
        if name not in self.devices:
            raise ValueError(f"Unknown Govee lights: {target}")
        return {name: self.devices[name]}

    async def _send(self, frame: bytes, target: str | None) -> dict:
        targets = self._targets(target)
        sent, errors = [], {}
        # Serialize radio use across devices and concurrent voice commands.
        async with self._lock:
            for name, address in targets.items():
                try:
                    device = None
                    for _ in range(4):
                        device = await BleakScanner.find_device_by_filter(
                            lambda d, adv: d.address.upper() == address
                            and name in (adv.local_name or d.name or "").upper(),
                            timeout=6.0,
                        )
                        if device is not None:
                            break
                    if device is None:
                        raise RuntimeError("Lights not found after four scans; check power and close the Govee app")
                    async with BleakClient(device, timeout=20.0) as client:
                        await client.write_gatt_char(WRITE_CHAR, frame)
                    sent.append(name)
                except BleakBluetoothNotAvailableError:
                    message = "Bluetooth is unavailable. Enable the PC Bluetooth radio and try again."
                    log.error(message)
                    errors.update({n: message for n in targets if n not in sent})
                    break
                except Exception as exc:
                    log.warning("Govee %s command failed: %s", name, exc)
                    errors[name] = str(exc)
        return {"sent": sent, "errors": errors, "ok": not errors}

    async def set_power(self, on: bool, target: str | None = None) -> dict:
        return await self._send(power(on), target)

    async def set_color(self, red: int, green: int, blue: int,
                        target: str | None = None) -> dict:
        return await self._send(color(red, green, blue), target)

    async def set_brightness(self, percent: int, target: str | None = None) -> dict:
        return await self._send(brightness(percent), target)
