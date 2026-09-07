"""Golden protocol bytes and BLE/voice integration without hardware."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import govee
from config import Config, ConfigError, load_config
from gemini_live import LiveSessionManager, _build_tools
from main import Hub


def test_golden_power():
    assert govee.power(True) == bytes.fromhex("33 01 01" + " 00" * 16 + " 33")
    assert govee.power(False) == bytes.fromhex("33 01 00" + " 00" * 16 + " 32")


def test_golden_red():
    assert govee.color(255, 0, 0) == bytes.fromhex(
        "33 05 15 01 ff 00 00 00 00 00 00 00 ff 7f 00 00 00 00 00 5d"
    )


@pytest.mark.parametrize("percent,checksum", [(0, 0x37), (50, 0x05), (100, 0x53)])
def test_brightness(percent, checksum):
    assert govee.brightness(percent) == bytes([0x33, 4, percent]) + bytes(16) + bytes([checksum])


@pytest.mark.parametrize("value", [-1, 256, 1.5, True, None])
def test_bad_rgb(value):
    with pytest.raises(ValueError):
        govee.color(value, 0, 0)


@pytest.mark.parametrize("value", [-1, 101, 1.5, True, None])
def test_bad_brightness(value):
    with pytest.raises(ValueError):
        govee.brightness(value)


@pytest.mark.parametrize("value", ["", "H617A", "H617A:bad", govee.DEFAULT_DEVICES + ",H617A:D2:21:C2:46:07:35"])
def test_bad_devices(value):
    with pytest.raises(ValueError):
        govee.GoveeHub(value)


@pytest.fixture
def ble(monkeypatch):
    device = SimpleNamespace(address="D2:21:C2:46:07:35", name="Govee_H617A_0735")
    scanner = AsyncMock(return_value=device)
    client = AsyncMock()
    client.__aenter__.return_value = client
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(govee.BleakScanner, "find_device_by_filter", scanner)
    monkeypatch.setattr(govee, "BleakClient", factory)
    return device, scanner, client, factory


async def test_broadcast_disconnect_and_filter(ble):
    device, scanner, client, factory = ble
    result = await govee.GoveeHub().set_color(255, 0, 0)
    assert result == {"sent": ["H617A", "H617C"], "errors": {}, "ok": True}
    assert client.__aexit__.await_count == 2
    assert factory.call_count == 2
    client.write_gatt_char.assert_awaited_with(govee.WRITE_CHAR, govee.color(255, 0, 0))
    # The second scan must select Patrick's address AND model name.
    predicate = scanner.call_args.args[0]
    assert not predicate(device, SimpleNamespace(local_name=device.name))
    assert predicate(SimpleNamespace(address="D0:05:C1:C6:78:40", name=None),
                     SimpleNamespace(local_name="Govee_H617C_7840"))


@pytest.mark.parametrize("target,model", [("jaiden lights", "H617A"), ("patrick lights", "H617C"), ("h617a", "H617A")])
async def test_alias(ble, target, model):
    result = await govee.GoveeHub().set_power(True, target)
    assert result["sent"] == [model]
    assert ble[2].__aexit__.await_count == 1


async def test_scan_retry(ble):
    ble[1].side_effect = [None, None, None, ble[0]]
    assert (await govee.GoveeHub().set_power(True, "jaiden lights"))["ok"]
    assert ble[1].await_count == 4
    assert ble[1].call_args.kwargs["timeout"] == 6


async def test_not_found_still_broadcasts(ble):
    ble[1].side_effect = [None] * 4 + [ble[0]]
    result = await govee.GoveeHub().set_power(False)
    assert result["sent"] == ["H617C"]
    assert "H617A" in result["errors"]
    assert not result["ok"]


async def test_write_failure_disconnects(ble):
    ble[2].write_gatt_char.side_effect = RuntimeError("write failed")
    result = await govee.GoveeHub().set_brightness(50)
    assert len(result["errors"]) == 2
    assert ble[2].__aexit__.await_count == 2


async def test_cancellation_disconnects(ble):
    ble[2].write_gatt_char.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await govee.GoveeHub().set_power(True)
    ble[2].__aexit__.assert_awaited_once()


async def test_radio_unavailable(ble, caplog):
    ble[1].side_effect = govee.BleakBluetoothNotAvailableError("off", None)
    result = await govee.GoveeHub().set_power(True)
    assert not result["ok"]
    assert len(result["errors"]) == 2
    assert "Enable the PC Bluetooth radio" in caplog.text
    ble[3].assert_not_called()


async def test_unknown_target_does_not_scan(ble):
    with pytest.raises(ValueError):
        await govee.GoveeHub().set_power(True, "unknown")
    ble[1].assert_not_awaited()


@pytest.mark.parametrize("enabled", [False, True])
def test_tool_declaration(enabled):
    declarations = _build_tools(Config(govee_enabled=enabled))[0].function_declarations
    assert ("govee_control" in [d.name for d in declarations]) == enabled


@pytest.mark.parametrize("action,args,method,expected", [
    ("on", {}, "set_power", (True, None)),
    ("off", {"target": "patrick lights"}, "set_power", (False, "patrick lights")),
    ("color", {"red": 255, "green": 0, "blue": 0}, "set_color", (255, 0, 0, None)),
    ("brightness", {"percent": 50}, "set_brightness", (50, None)),
])
async def test_voice_dispatch(action, args, method, expected):
    hub = Hub.__new__(Hub)
    hub.govee = SimpleNamespace(**{method: AsyncMock(return_value={"ok": True})})
    manager = LiveSessionManager(Config(govee_enabled=True), hub)
    result = await manager._run_tool(SimpleNamespace(name="govee_control", args={"action": action, **args}))
    assert result == {"ok": True}
    getattr(hub.govee, method).assert_awaited_once_with(*expected)


async def test_disabled_and_invalid_voice(ble):
    hub = Hub.__new__(Hub)
    hub.govee = None
    assert "error" in await hub.govee_control("on")
    hub.govee = govee.GoveeHub()
    assert "error" in await hub.govee_control("color", red=255)
    assert "error" in await hub.govee_control("invalid")
    ble[1].assert_not_awaited()


def test_config_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ECHO_SHARED_SECRET", "test-secret-long-enough")
    monkeypatch.setenv("GOVEE_ENABLED", "true")
    monkeypatch.setenv("GOVEE_DEVICES", "H617A:D2:21:C2:46:07:35")
    cfg = load_config(tmp_path / "missing.env")
    assert cfg.govee_enabled
    assert cfg.govee_devices == "H617A:D2:21:C2:46:07:35"
    monkeypatch.setenv("GOVEE_DEVICES", "bad")
    with pytest.raises(ConfigError, match="GOVEE_DEVICES"):
        load_config(tmp_path / "missing.env")
