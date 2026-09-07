"""v1.6 physical-button behaviour (HARDWARE-BRIEF-7 v2): the mic button is the
manual session trigger and the mute control; screen taps are UI-only."""

from __future__ import annotations

from main import HUB_KEY  # noqa: F401  (fixtures import it)
from protocol import Button, Mute, Tap
from test_display_api import FakeDevice, client, handled, tap_device  # shared helpers


async def test_button_talk_toggle_starts_a_session_when_idle(client) -> None:
    device, session = await tap_device(client, session_active=False)
    await device.ws.send_str(Button(action="talk_toggle").encode())
    await handled(device)
    assert session.starts == ["button"], "a short press when idle opens a session"
    assert session.stops == []


async def test_button_talk_toggle_ends_an_active_session(client) -> None:
    device, session = await tap_device(client, session_active=True)
    await device.ws.send_str(Button(action="talk_toggle").encode())
    await handled(device)
    assert session.stops, "a short press during a session ends it"
    assert session.starts == [], "and never opens a replacement"


async def test_button_mute_flips_the_server_mirror(client) -> None:
    device, session = await tap_device(client, session_active=False)
    hub = client.app[HUB_KEY]
    assert hub.muted is False
    await device.ws.send_str(Button(action="mute").encode())
    await handled(device)
    assert hub.muted is True, "the server mirror follows the device mute"
    await device.ws.send_str(Button(action="mute").encode())
    await handled(device)
    assert hub.muted is False
    assert session.starts == [], "mute toggling never opens a session"


async def test_button_talk_toggle_unmutes_and_talks(client) -> None:
    """A short press while muted unmutes AND starts talking (owner default (a))."""
    device, session = await tap_device(client, session_active=False)
    await device.ws.send_str(Button(action="mute").encode())
    await handled(device)
    assert client.app[HUB_KEY].muted is True
    await device.ws.send_str(Button(action="talk_toggle").encode())
    await handled(device)
    assert client.app[HUB_KEY].muted is False, "talk_toggle clears the mute mirror"
    assert session.starts == ["button"]


async def test_an_idle_tap_never_opens_a_session(client) -> None:
    """v1.6: the screen no longer starts conversations -- only UI now."""
    device, session = await tap_device(client, session_active=False)
    await device.ws.send_str(Tap().encode())
    await handled(device)
    assert session.starts == []
    assert session.stops == []


async def test_mute_message_from_the_server_reaches_the_device(client) -> None:
    """set_mute pushes the full desired state to the device, never a toggle."""
    device = await FakeDevice.connect(client)
    hub = client.app[HUB_KEY]
    await hub.set_mute(True)
    msg = await device.expect(Mute)
    assert msg.on is True
    assert hub.muted is True
    await hub.set_mute(False)
    msg = await device.expect(Mute)
    assert msg.on is False
    assert hub.muted is False
