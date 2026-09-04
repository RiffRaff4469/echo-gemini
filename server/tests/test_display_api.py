"""End-to-end display API + WebSocket hub, with a real device socket attached.

These run the actual aiohttp app over a real loopback socket, so what is under
test is the whole path a ``POST /display`` takes: auth, validation, routing onto
the device link, and the frame the device actually receives.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

import protocol as P
from config import Config
from main import DISPLAY_SECRET_HEADER, SECRET_HEADER, build_app

SECRET = "test-secret-not-a-real-one"


def make_config(**overrides) -> Config:
    cfg = Config(
        shared_secret=SECRET,
        wake_enabled=False,
        wake_download_models=False,
        heartbeat_interval_s=30.0,
        gemini_api_key="",  # Live leg off: display path must work regardless
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture
async def client(request):
    cfg = getattr(request, "param", None) or make_config()
    app = build_app(cfg)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


class FakeDevice:
    """Minimal in-test device: connects, says hello, records what arrives."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws

    @classmethod
    async def connect(cls, client: TestClient, device_id: str = "test-device"):
        ws = await client.ws_connect("/ws", headers={SECRET_HEADER: SECRET})
        device = cls(ws)
        await ws.send_str(P.Hello(device_id=device_id, app_version="test").encode())
        # Drain the full greeting -- welcome, mic, idle state -- so a test that
        # asserts on later messages is not handed the handshake's.
        await device.expect(P.Welcome)
        await device.expect(P.Mic)
        await device.expect(P.StateMsg)
        return device

    async def expect(self, msg_type: type, timeout: float = 3.0):
        """Wait for the next control message of ``msg_type``, skipping others."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for {msg_type.__name__}")
            raw = await asyncio.wait_for(self.ws.receive(), timeout=remaining)
            if raw.type is aiohttp.WSMsgType.TEXT:
                msg = P.decode(raw.data)
                if isinstance(msg, msg_type):
                    return msg
            elif raw.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.ERROR,
            ):
                raise AssertionError(f"socket closed waiting for {msg_type.__name__}")


# --- handshake --------------------------------------------------------------


async def test_websocket_requires_the_shared_secret(client: TestClient) -> None:
    with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
        await client.ws_connect("/ws")
    assert exc.value.status == 401


async def test_websocket_rejects_a_wrong_secret(client: TestClient) -> None:
    with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
        await client.ws_connect("/ws", headers={SECRET_HEADER: "not-the-secret"})
    assert exc.value.status == 401


async def test_hello_gets_welcome_mic_and_state(client: TestClient) -> None:
    ws = await client.ws_connect("/ws", headers={SECRET_HEADER: SECRET})
    await ws.send_str(P.Hello(device_id="checkers-01", app_version="0.1.0").encode())
    device = FakeDevice(ws)
    welcome = await device.expect(P.Welcome)
    assert welcome.audio_up_rate == P.AUDIO_UP_RATE
    assert welcome.audio_down_rate == P.AUDIO_DOWN_RATE
    assert welcome.live_enabled is False  # no key configured
    assert (await device.expect(P.Mic)).enabled is True
    assert (await device.expect(P.StateMsg)).state is P.UiState.IDLE
    await ws.close()


async def test_protocol_version_mismatch_is_refused(client: TestClient) -> None:
    ws = await client.ws_connect("/ws", headers={SECRET_HEADER: SECRET})
    await ws.send_str(
        P.Hello(device_id="old-client", protocol_version=P.PROTOCOL_VERSION + 1).encode()
    )
    device = FakeDevice(ws)
    err = await device.expect(P.ErrorMsg)
    assert err.code == "protocol_mismatch"


async def test_second_connection_replaces_the_first(client: TestClient) -> None:
    """A device that drops off Wi-Fi and redials must not leave a half-open
    socket behind, and must not be locked out by its own zombie."""
    first = await FakeDevice.connect(client, "checkers-01")
    second = await FakeDevice.connect(client, "checkers-01")

    # The old socket is closed by the server, not left dangling.
    for _ in range(10):
        raw = await asyncio.wait_for(first.ws.receive(), timeout=3.0)
        if raw.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
            break
    else:
        raise AssertionError("first link was never closed")

    # The new link is fully live.
    resp = await client.post(
        "/display",
        json={"type": "text", "payload": {"text": "still here"}},
        headers={DISPLAY_SECRET_HEADER: SECRET},
    )
    assert resp.status == 200
    assert (await second.expect(P.Display)).command.payload["text"] == "still here"


async def test_garbage_control_message_gets_an_error_not_a_disconnect(
    client: TestClient,
) -> None:
    device = await FakeDevice.connect(client)
    await device.ws.send_str("{ this is not json")
    err = await device.expect(P.ErrorMsg)
    assert err.code == "bad_message"
    assert not device.ws.closed


async def test_device_ping_is_answered_with_pong(client: TestClient) -> None:
    device = await FakeDevice.connect(client)
    await device.ws.send_str(P.Ping(nonce=99).encode())
    assert (await device.expect(P.Pong)).nonce == 99


async def test_malformed_binary_frame_is_ignored_not_fatal(client: TestClient) -> None:
    device = await FakeDevice.connect(client)
    await device.ws.send_bytes(b"\xff\xff\xff")
    await device.ws.send_str(P.Ping(nonce=5).encode())
    assert (await device.expect(P.Pong)).nonce == 5


# --- POST /display ----------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"type": "text", "payload": {"text": "Good morning"}, "duration": 20},
        {"type": "html", "payload": {"html": "<b>hi</b>"}, "priority": 2},
        {"type": "image", "payload": {"url": "https://example.com/cat.jpg"}},
        {"type": "timer", "payload": {"label": "Eggs", "seconds": 360}},
    ],
)
async def test_every_display_type_reaches_the_device(
    client: TestClient, body: dict
) -> None:
    device = await FakeDevice.connect(client)
    resp = await client.post(
        "/display", json=body, headers={DISPLAY_SECRET_HEADER: SECRET}
    )
    assert resp.status == 200
    assert (await resp.json())["ok"] is True

    display = await device.expect(P.Display)
    assert display.command.type.value == body["type"]
    assert display.command.payload == body["payload"]
    assert display.command.duration == body.get("duration", 0)
    assert display.command.priority == body.get("priority", 0)


async def test_display_returns_503_when_no_device_is_connected(
    client: TestClient,
) -> None:
    resp = await client.post(
        "/display",
        json={"type": "text", "payload": {"text": "nobody home"}},
        headers={DISPLAY_SECRET_HEADER: SECRET},
    )
    assert resp.status == 503
    assert (await resp.json())["device_connected"] is False


async def test_display_requires_the_shared_secret(client: TestClient) -> None:
    await FakeDevice.connect(client)
    resp = await client.post(
        "/display", json={"type": "text", "payload": {"text": "x"}}
    )
    assert resp.status == 401


async def test_display_accepts_the_ws_secret_header_too(client: TestClient) -> None:
    await FakeDevice.connect(client)
    resp = await client.post(
        "/display",
        json={"type": "text", "payload": {"text": "x"}},
        headers={SECRET_HEADER: SECRET},
    )
    assert resp.status == 200


@pytest.mark.parametrize(
    "client", [make_config(display_require_secret=False)], indirect=True
)
async def test_display_secret_can_be_disabled_by_config(client: TestClient) -> None:
    await FakeDevice.connect(client)
    resp = await client.post(
        "/display", json={"type": "text", "payload": {"text": "x"}}
    )
    assert resp.status == 200


@pytest.mark.parametrize(
    "body,expected_fragment",
    [
        ({"type": "nope", "payload": {}}, "type must be one of"),
        ({"type": "text"}, "payload must be"),
        ({"type": "text", "payload": {}}, "payload.text"),
        ({"type": "timer", "payload": {"label": "x"}}, "payload.seconds"),
        ({"type": "image", "payload": {"url": "javascript:alert(1)"}}, "payload.url"),
        ({"type": "text", "payload": {"text": "x"}, "duration": -3}, "duration"),
    ],
)
async def test_invalid_display_bodies_get_400_with_a_reason(
    client: TestClient, body: dict, expected_fragment: str
) -> None:
    await FakeDevice.connect(client)
    resp = await client.post(
        "/display", json=body, headers={DISPLAY_SECRET_HEADER: SECRET}
    )
    assert resp.status == 400
    assert expected_fragment in (await resp.json())["error"]


async def test_non_json_body_gets_400(client: TestClient) -> None:
    await FakeDevice.connect(client)
    resp = await client.post(
        "/display",
        data="not json",
        headers={DISPLAY_SECRET_HEADER: SECRET, "Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_display_clear_reaches_the_device(client: TestClient) -> None:
    device = await FakeDevice.connect(client)
    resp = await client.post(
        "/display/clear", headers={DISPLAY_SECRET_HEADER: SECRET}
    )
    assert resp.status == 200
    await device.expect(P.DisplayClear)


# --- /vision ----------------------------------------------------------------


async def test_vision_requires_an_active_session(client: TestClient) -> None:
    await FakeDevice.connect(client)
    resp = await client.post(
        "/vision", json={"enabled": True}, headers={DISPLAY_SECRET_HEADER: SECRET}
    )
    assert resp.status == 409
    assert "no active session" in (await resp.json())["error"]


@pytest.mark.parametrize("client", [make_config(vision_mode="off")], indirect=True)
async def test_vision_off_mode_refuses_to_open_the_camera(client: TestClient) -> None:
    await FakeDevice.connect(client)
    resp = await client.post(
        "/vision", json={"enabled": True}, headers={DISPLAY_SECRET_HEADER: SECRET}
    )
    assert resp.status == 409


async def test_vision_rejects_a_non_boolean(client: TestClient) -> None:
    await FakeDevice.connect(client)
    resp = await client.post(
        "/vision", json={"enabled": "yes"}, headers={DISPLAY_SECRET_HEADER: SECRET}
    )
    assert resp.status == 400


# --- /health ----------------------------------------------------------------


async def test_health_is_unauthenticated_and_reports_no_device(
    client: TestClient,
) -> None:
    body = await (await client.get("/health")).json()
    assert body["ok"] is True
    assert body["device_connected"] is False
    assert body["live_enabled"] is False  # no GEMINI_API_KEY -> Live disabled
    assert body["session_active"] is False


async def test_health_reports_the_connected_device(client: TestClient) -> None:
    await FakeDevice.connect(client, "checkers-01")
    body = await (await client.get("/health")).json()
    assert body["device_connected"] is True
    assert body["device"]["device_id"] == "checkers-01"


async def test_health_never_leaks_the_secret_or_the_api_key(
    client: TestClient,
) -> None:
    raw = await (await client.get("/health")).text()
    assert SECRET not in raw
    assert "api_key" not in json.loads(raw)


# --- media routing ----------------------------------------------------------


async def test_mic_audio_without_a_session_is_absorbed_by_the_wake_engine(
    client: TestClient,
) -> None:
    """Wake word is disabled in this config, so audio must be consumed silently
    rather than opening a session or erroring."""
    device = await FakeDevice.connect(client)
    frame = P.MediaFrame(channel=P.Channel.AUDIO_UP, payload=b"\x00" * 640, seq=1)
    for _ in range(20):
        await device.ws.send_bytes(frame.encode())
    await device.ws.send_str(P.Ping(nonce=1).encode())
    assert (await device.expect(P.Pong)).nonce == 1


async def test_tap_without_a_key_still_drives_ui_state(client: TestClient) -> None:
    """Tap-to-talk is a permanent override. With Live disabled it must not
    raise -- it should light up and fall back to idle."""
    device = await FakeDevice.connect(client)
    await device.ws.send_str(P.Tap(pressed=True).encode())
    assert (await device.expect(P.StateMsg)).state is P.UiState.LISTENING
    assert (await device.expect(P.StateMsg)).state is P.UiState.IDLE
    assert not device.ws.closed


async def test_camera_status_from_device_is_accepted(client: TestClient) -> None:
    device = await FakeDevice.connect(client)
    await device.ws.send_str(
        P.CameraStatusMsg(
            status=P.CameraStatus.SHUTTER_CLOSED, detail="frames near-black"
        ).encode()
    )
    await device.ws.send_str(P.Ping(nonce=3).encode())
    assert (await device.expect(P.Pong)).nonce == 3
