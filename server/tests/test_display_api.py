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
from main import DISPLAY_SECRET_HEADER, HUB_KEY, SECRET_HEADER, build_app

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


# --- tap dispatch -----------------------------------------------------------
#
# One gesture, two meanings, and the device picks which envelope to send from
# what is on its own screen. What the hub does with each is pinned here because
# FIX-BRIEF-11 inverted the second one: a tap during a session used to buy
# another half minute and now ends the conversation on the spot.


class StubSession:
    """Stands in for ``LiveSessionManager`` -- these tests run with no API key,
    and what is under test is the hub's dispatch, not the Live leg."""

    def __init__(self, active: bool = False) -> None:
        self.active = active
        self.starts: list[str] = []
        self.stops: list[str] = []
        self.touches = 0
        self.quiet_seconds_left: float | None = None
        self.vision_on = False
        # UI-BRIEF-16: choices queued into the session (select taps).
        self.choices: list[str] = []

    async def start(self, reason: str) -> bool:
        self.starts.append(reason)
        self.active = True
        return True

    async def stop(self, reason: str) -> None:
        self.stops.append(reason)
        self.active = False

    def choose(self, text: str) -> bool:
        if not self.active:
            return False
        self.choices.append(text)
        return True

    def touch(self) -> None:
        self.touches += 1


async def tap_device(
    client: TestClient, *, session_active: bool
) -> tuple[FakeDevice, StubSession]:
    device = await FakeDevice.connect(client)
    session = StubSession(active=session_active)
    client.app[HUB_KEY].session = session
    return device, session


async def handled(device: FakeDevice, nonce: int = 1) -> None:
    """Block until the hub has finished the message just sent.

    The link reads one control message at a time, so a pong for a ping sent
    afterwards cannot come back until the earlier handler has returned.
    """
    await device.ws.send_str(P.Ping(nonce=nonce).encode())
    assert (await device.expect(P.Pong)).nonce == nonce


async def test_a_tap_with_nothing_running_opens_a_session(client: TestClient) -> None:
    # v1.6 (HARDWARE-BRIEF-7 v2): screen taps are UI-only -- they no longer
    # start or stop conversations. The wake word and the physical mic button
    # (button talk_toggle) are the triggers.
    device, session = await tap_device(client, session_active=False)
    await device.ws.send_str(P.Tap().encode())
    await handled(device)
    assert session.starts == [], "v1.6: an idle tap must NOT open a session"
    assert session.stops == []


async def test_a_stop_ends_the_running_session(client: TestClient) -> None:
    device, session = await tap_device(client, session_active=True)
    await device.ws.send_str(P.Stop().encode())
    await handled(device)
    assert session.stops, "a tap during a session must close it"
    assert session.starts == [], "and must never open a replacement"


async def test_a_stop_that_races_the_close_does_nothing(client: TestClient) -> None:
    """A finger landing the instant the session closes on its own. Under v1.3
    this opened a fresh one, because a tap then meant "keep going". It now means
    "stop", and it already has."""
    device, session = await tap_device(client, session_active=False)
    await device.ws.send_str(P.Stop().encode())
    await handled(device)
    assert session.starts == []
    assert session.stops == []


async def test_an_old_clients_stay_is_treated_as_a_stop(client: TestClient) -> None:
    """A device that has not been reflashed still speaks v1.3. Owner semantics
    supersede the old ones rather than the tap being dropped on the floor."""
    device, session = await tap_device(client, session_active=True)
    await device.ws.send_str(P.Stay().encode())
    await handled(device)
    assert session.stops, "v1.3's stay must end the session, not extend it"


# --- POST /display ----------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"type": "text", "payload": {"text": "Good morning"}, "duration": 20},
        {"type": "html", "payload": {"html": "<b>hi</b>"}, "priority": 2},
        {"type": "image", "payload": {"url": "https://example.com/cat.jpg"}},
        {"type": "timer", "payload": {"label": "Eggs", "seconds": 360}},
        {
            "type": "now_playing",
            "payload": {
                "title": "Teardrop",
                "artist": "Massive Attack",
                "art_url": "https://example.com/art.jpg",
                "progress_s": 42,
                "duration_s": 330,
                "is_playing": True,
            },
        },
        # v1.6 panels (UI-BRIEF-16).
        {
            "type": "options",
            "payload": {
                "question": "Pick one",
                "options": [{"label": "Alpha"}, {"label": "Beta"}, {"label": "Gamma"}],
            },
            "priority": 1,
        },
        {
            "type": "list",
            "payload": {"title": "Groceries", "items": ["Milk", "Eggs", "Bread"]},
        },
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
    """v1.6: an idle tap no longer opens a session -- it is ignored entirely.
    The device stays idle and the link stays open."""
    device = await FakeDevice.connect(client)
    await device.ws.send_str(P.Tap(pressed=True).encode())
    # No state messages should arrive: nothing opened, nothing closed.
    assert not device.ws.closed
    assert (await device.expect(P.AlarmCommand))  # the connect-time list request


# --- alarms over the real socket --------------------------------------------


async def test_the_server_asks_for_the_schedule_on_connect(client: TestClient) -> None:
    """So /health and the model's first list_alarms have something to report
    without waiting for a round trip mid-conversation."""
    device = await FakeDevice.connect(client)
    command = await device.expect(P.AlarmCommand)
    assert command.op is P.AlarmOp.LIST


async def test_alarm_state_from_the_device_reaches_health(client: TestClient) -> None:
    device = await FakeDevice.connect(client)
    await device.ws.send_str(
        P.AlarmStateMsg(
            alarms=[
                P.AlarmEntry(
                    id="a1",
                    kind=P.AlarmKind.ALARM,
                    label="Wake up",
                    time_epoch_ms=1_800_000_000_000,
                )
            ]
        ).encode()
    )
    # Round-trip a ping so the state has certainly been processed.
    await device.ws.send_str(P.Ping(nonce=11).encode())
    await device.expect(P.Pong)

    body = await (await client.get("/health")).json()
    assert body["alarms"]["alarms"][0]["label"] == "Wake up"


async def test_health_reports_no_alarms_before_the_device_says_anything(
    client: TestClient,
) -> None:
    body = await (await client.get("/health")).json()
    assert body["alarms"] is None


async def test_alarm_fired_without_a_session_is_accepted_quietly(
    client: TestClient,
) -> None:
    """The device is already chiming locally. With no Live session there is
    nothing to tell, and that must not be an error."""
    device = await FakeDevice.connect(client)
    await device.ws.send_str(
        P.AlarmFired(kind=P.AlarmKind.TIMER, id="t1", label="Pasta").encode()
    )
    await device.ws.send_str(P.Ping(nonce=12).encode())
    assert (await device.expect(P.Pong)).nonce == 12
    assert not device.ws.closed


async def test_an_alarm_command_reaches_the_device(client: TestClient) -> None:
    device = await FakeDevice.connect(client)
    hub = client.app[HUB_KEY]
    assert hub.push_alarm_command(
        P.AlarmCommand(op=P.AlarmOp.SET_TIMER, label="Pasta", duration_s=600)
    )
    # The connect-time LIST is drained by expect()'s skipping, so this is ours.
    sent = await device.expect(P.AlarmCommand)
    while sent.op is P.AlarmOp.LIST:
        sent = await device.expect(P.AlarmCommand)
    assert sent.label == "Pasta" and sent.duration_s == 600


async def test_alarm_commands_report_offline_when_no_device_is_attached(
    client: TestClient,
) -> None:
    hub = client.app[HUB_KEY]
    assert hub.push_alarm_command(P.AlarmCommand(op=P.AlarmOp.LIST)) is False


async def test_alarm_tool_round_trip_waits_for_correlated_socket_ack(client: TestClient):
    from test_alarms import FakeSchedule
    device = await FakeDevice.connect(client)
    await device.expect(P.AlarmCommand)  # greeting LIST
    hub = client.app[HUB_KEY]
    call = type("Call", (), {"name": "set_timer", "args": {"duration_s": 60, "label": "Tea"}})()
    task = asyncio.create_task(hub.session._run_tool(call))
    command = await device.expect(P.AlarmCommand)
    schedule = FakeSchedule()
    assert not schedule.apply(command)
    alarms, timers = schedule.snapshot()
    await device.ws.send_str(P.AlarmStateMsg(alarms=alarms, timers=timers).encode())
    await device.ws.send_str(P.Ping(nonce=19).encode())
    await device.expect(P.Pong)
    assert not task.done(), "An unsolicited snapshot must not confirm a tool"
    await device.ws.send_str(P.AlarmStateMsg(alarms=alarms, timers=timers, req_id=command.req_id).encode())
    result = await asyncio.wait_for(task, 1)
    assert result["ok"] and result["timers"][0]["label"] == "Tea"


async def test_now_playing_routes_over_socket(client: TestClient):
    device = await FakeDevice.connect(client)
    body = {"type": "now_playing", "payload": {"title": "Example", "artist": "Artist", "progress_s": 5, "duration_s": 90}}
    response = await client.post("/display", json=body, headers={DISPLAY_SECRET_HEADER: SECRET})
    assert response.status == 200
    message = await device.expect(P.Display)
    assert message.command.payload == body["payload"]


async def test_camera_status_from_device_is_accepted(client: TestClient) -> None:
    device = await FakeDevice.connect(client)
    await device.ws.send_str(
        P.CameraStatusMsg(
            status=P.CameraStatus.SHUTTER_CLOSED, detail="frames near-black"
        ).encode()
    )
    await device.ws.send_str(P.Ping(nonce=3).encode())
    assert (await device.expect(P.Pong)).nonce == 3
