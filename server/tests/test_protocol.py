"""Envelope, media frame and display-command round-trips.

The Kotlin client hand-mirrors this schema, so anything these tests pin down is
also a promise to ``EchoTerminal/.../Protocol.kt``.
"""

from __future__ import annotations

import json

import pytest

import protocol as P


# --- control messages -------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        P.Hello(device_id="checkers-01", app_version="0.1.0", capabilities={"camera": True}),
        P.Pong(nonce=7),
        P.Tap(pressed=True),
        P.Tap(pressed=False),
        P.CameraStatusMsg(status=P.CameraStatus.SHUTTER_CLOSED, detail="near-black"),
        P.DeviceLog(level="warn", message="battery? there is none"),
        P.ErrorMsg(code="camera_open_failed", message="in use"),
        P.Welcome(heartbeat_interval_s=15.0, live_enabled=True),
        P.Ping(nonce=42),
        P.StateMsg(state=P.UiState.SPEAKING),
        P.Mic(enabled=False, gain=2.5),
        P.Mic(enabled=True),
        P.Interrupt(),
        P.DisplayClear(),
        P.Video(enabled=True, fps=1.0, width=768, height=768, jpeg_quality=70),
        P.AlarmCommand(op=P.AlarmOp.LIST),
        P.AlarmCommand(
            op=P.AlarmOp.SET_ALARM,
            label="Wake up",
            time_epoch_ms=1_800_000_000_000,
            days=["mon", "wed", "fri"],
            req_id="abc123",
        ),
        P.AlarmCommand(op=P.AlarmOp.SET_TIMER, label="Pasta", duration_s=600),
        P.AlarmCommand(op=P.AlarmOp.CANCEL, id="a1", kind="alarm"),
        P.AlarmFired(kind=P.AlarmKind.TIMER, id="t2", label="Pasta"),
    ],
)
def test_control_message_round_trip(msg: P.Message) -> None:
    decoded = P.decode(msg.encode())
    assert type(decoded) is type(msg)
    assert decoded.fields() == msg.fields()


def test_display_message_round_trip() -> None:
    cmd = P.DisplayCommand(
        type=P.DisplayType.TEXT,
        payload={"text": "Bus in 4 min", "subtitle": "Route 62"},
        duration=30,
        priority=5,
    )
    decoded = P.decode(P.Display(command=cmd).encode())
    assert isinstance(decoded, P.Display)
    assert decoded.command.type is P.DisplayType.TEXT
    assert decoded.command.payload == cmd.payload
    assert decoded.command.duration == 30
    assert decoded.command.priority == 5


def test_envelope_carries_version_type_and_timestamp() -> None:
    body = json.loads(P.Ping(nonce=1).encode())
    assert body["v"] == P.PROTOCOL_VERSION
    assert body["t"] == "ping"
    assert body["ts"] > 0


def test_explicit_timestamp_is_preserved() -> None:
    assert json.loads(P.Ping(nonce=1, ts=1234).encode())["ts"] == 1234


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "[]",
        '{"t":"ping"}',                      # no version
        '{"v":99,"t":"ping"}',               # wrong version
        '{"v":1,"t":"nope"}',                # unknown type
        '{"v":1,"t":"hello"}',               # missing device_id
        '{"v":1,"t":"hello","device_id":""}',
        '{"v":1,"t":"state","state":"dancing"}',
        '{"v":1,"t":"camera_status","status":"melted"}',
    ],
)
def test_malformed_control_messages_raise_protocol_error(raw: str) -> None:
    with pytest.raises(P.ProtocolError):
        P.decode(raw)


def test_video_fps_is_clamped_to_the_api_cap() -> None:
    # The Live API caps video at 1 FPS; anything above is wasted device work.
    assert P.Video(enabled=True, fps=15.0).fps == P.VIDEO_MAX_FPS


# --- binary media frames ----------------------------------------------------


@pytest.mark.parametrize(
    "channel", [P.Channel.AUDIO_UP, P.Channel.AUDIO_DOWN, P.Channel.VIDEO_UP]
)
def test_media_frame_round_trip(channel: P.Channel) -> None:
    payload = bytes(range(256)) * 3
    frame = P.MediaFrame(channel=channel, payload=payload, seq=65_537, flags=0)
    decoded = P.MediaFrame.decode(frame.encode())
    assert decoded.channel is channel
    assert decoded.payload == payload
    assert decoded.seq == 65_537


def test_media_frame_header_is_eight_bytes() -> None:
    frame = P.MediaFrame(channel=P.Channel.AUDIO_UP, payload=b"")
    assert len(frame.encode()) == P.HEADER_SIZE == 8


def test_empty_payload_round_trips() -> None:
    decoded = P.MediaFrame.decode(
        P.MediaFrame(channel=P.Channel.VIDEO_UP, payload=b"").encode()
    )
    assert decoded.payload == b""


def test_sequence_counter_is_per_channel_and_wraps() -> None:
    seq = P.SeqCounter()
    assert seq.next(P.Channel.AUDIO_UP) == 0
    assert seq.next(P.Channel.AUDIO_UP) == 1
    assert seq.next(P.Channel.AUDIO_DOWN) == 0  # independent counters
    seq._counters[P.Channel.VIDEO_UP] = 0xFFFFFFFF
    assert seq.next(P.Channel.VIDEO_UP) == 0xFFFFFFFF
    assert seq.next(P.Channel.VIDEO_UP) == 0


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x01\x01\x00",                          # truncated header
        b"\x02\x01\x00\x00\x00\x00\x00\x00",      # bad version
        b"\x01\x7f\x00\x00\x00\x00\x00\x00",      # unknown channel
    ],
)
def test_malformed_media_frames_raise_protocol_error(raw: bytes) -> None:
    with pytest.raises(P.ProtocolError):
        P.MediaFrame.decode(raw)


def test_audio_frame_constants_match_the_live_api_spec() -> None:
    # 16 kHz in / 24 kHz out, PCM16 mono -- pass-through both ways, no resample.
    assert (P.AUDIO_UP_RATE, P.AUDIO_DOWN_RATE) == (16_000, 24_000)
    assert P.AUDIO_CHANNELS == 1 and P.AUDIO_SAMPLE_WIDTH == 2
    assert P.AUDIO_UP_FRAME_BYTES == 640  # 20 ms


# --- display command validation --------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"type": "text", "payload": {"text": "hello"}},
        {"type": "text", "payload": {"text": "hi", "subtitle": "there"}, "duration": 10},
        {"type": "html", "payload": {"html": "<h1>x</h1>"}, "priority": 3},
        {"type": "image", "payload": {"url": "https://example.com/a.png"}},
        {"type": "image", "payload": {"url": "data:image/png;base64,AAAA"}},
        {"type": "timer", "payload": {"label": "Pasta", "seconds": 480}},
        {"type": "now_playing", "payload": {"title": "Teardrop"}},
        {
            "type": "now_playing",
            "payload": {
                "title": "Teardrop",
                "artist": "Massive Attack",
                "album": "Mezzanine",
                "art_url": "https://example.com/art.jpg",
                "progress_s": 42,
                "duration_s": 330,
                "is_playing": True,
            },
        },
    ],
)
def test_valid_display_commands_accepted(body: dict) -> None:
    cmd = P.DisplayCommand.from_dict(body)
    assert cmd.type.value == body["type"]


@pytest.mark.parametrize(
    "body",
    [
        "a string",
        {},                                                   # no type
        {"type": "video", "payload": {}},                     # not an allowed type
        {"type": "text", "payload": "not an object"},
        {"type": "text", "payload": {}},                      # missing text
        {"type": "text", "payload": {"text": ""}},            # empty text
        {"type": "text", "payload": {"text": "x", "subtitle": 5}},
        {"type": "html", "payload": {}},
        {"type": "image", "payload": {"url": "ftp://nope/a.png"}},
        {"type": "image", "payload": {"url": "javascript:alert(1)"}},
        {"type": "timer", "payload": {"label": "x"}},         # missing seconds
        {"type": "timer", "payload": {"label": "x", "seconds": 0}},
        {"type": "timer", "payload": {"label": "x", "seconds": -5}},
        {"type": "text", "payload": {"text": "x"}, "duration": -1},
        {"type": "text", "payload": {"text": "x"}, "duration": 10**9},
        {"type": "text", "payload": {"text": "x"}, "duration": "soon"},
        {"type": "text", "payload": {"text": "x"}, "priority": "high"},
        {"type": "text", "payload": {"text": "x"}, "priority": 1.5},
        {"type": "now_playing", "payload": {}},                    # missing title
        {"type": "now_playing", "payload": {"title": "x", "artist": 7}},
        {"type": "now_playing", "payload": {"title": "x", "art_url": "ftp://a/b.jpg"}},
        {"type": "now_playing", "payload": {"title": "x", "progress_s": -1}},
        {"type": "now_playing", "payload": {"title": "x", "duration_s": 0}},
        {"type": "now_playing", "payload": {"title": "x", "is_playing": "yes"}},
    ],
)
def test_invalid_display_commands_rejected(body) -> None:
    with pytest.raises(P.ProtocolError):
        P.DisplayCommand.from_dict(body)


def test_booleans_are_not_accepted_as_numbers() -> None:
    # bool is an int subclass in Python; a JSON `true` duration is a client bug.
    with pytest.raises(P.ProtocolError):
        P.DisplayCommand.from_dict(
            {"type": "text", "payload": {"text": "x"}, "duration": True}
        )


def test_oversized_html_is_rejected() -> None:
    huge = "x" * (P.MAX_HTML_BYTES + 1)
    with pytest.raises(P.ProtocolError):
        P.DisplayCommand.from_dict({"type": "html", "payload": {"html": huge}})


# --- alarms and timers (v1.1) -----------------------------------------------


def test_alarm_state_round_trip_keeps_every_field() -> None:
    state = P.AlarmStateMsg(
        alarms=[
            P.AlarmEntry(
                id="a1",
                kind=P.AlarmKind.ALARM,
                label="Wake up",
                time_epoch_ms=1_800_000_000_000,
                days=["mon", "tue"],
            )
        ],
        timers=[
            P.AlarmEntry(
                id="t1",
                kind=P.AlarmKind.TIMER,
                label="Pasta",
                duration_s=600,
                remaining_s=421.5,
                ringing=False,
            )
        ],
        req_id="r7",
        exact_allowed=False,
        error="",
    )
    decoded = P.decode(state.encode())
    assert isinstance(decoded, P.AlarmStateMsg)
    assert decoded.fields() == state.fields()
    assert decoded.alarms[0].days == ["mon", "tue"]
    assert decoded.timers[0].remaining_s == pytest.approx(421.5)
    assert decoded.exact_allowed is False


def test_days_are_normalised_to_monday_first_order() -> None:
    # Both sides must agree on order, or "every Sunday, Monday" reads back
    # differently than it was set.
    cmd = P.AlarmCommand(
        op=P.AlarmOp.SET_ALARM, time_epoch_ms=1, days=["sun", "MON", "sun"]
    )
    assert cmd.days == ["mon", "sun"]


def test_protocol_minor_is_announced_and_defaults_to_zero_for_old_peers() -> None:
    assert P.decode(P.Hello(device_id="d").encode()).protocol_minor == P.PROTOCOL_MINOR
    # A v1.0 device omits the field entirely; that is skew, not corruption.
    old = P.decode('{"v":1,"t":"hello","device_id":"d"}')
    assert old.protocol_minor == 0


@pytest.mark.parametrize(
    "raw",
    [
        '{"v":1,"t":"alarm_command","op":"detonate"}',
        '{"v":1,"t":"alarm_command","op":"set_alarm","time_epoch_ms":0}',
        '{"v":1,"t":"alarm_command","op":"set_timer","duration_s":0}',
        '{"v":1,"t":"alarm_command","op":"set_timer","duration_s":99999999}',
        '{"v":1,"t":"alarm_command","op":"list","days":["funday"]}',
        '{"v":1,"t":"alarm_command","op":"cancel","kind":"reminder"}',
        '{"v":1,"t":"alarm_fired","kind":"earthquake"}',
        '{"v":1,"t":"alarm_state","alarms":[{"kind":"alarm"}]}',      # no id
        '{"v":1,"t":"alarm_state","alarms":[{"id":"a","kind":"nope"}]}',
    ],
)
def test_malformed_alarm_messages_raise_protocol_error(raw: str) -> None:
    with pytest.raises(P.ProtocolError):
        P.decode(raw)


def test_alarm_labels_are_bounded() -> None:
    cmd = P.AlarmCommand(op=P.AlarmOp.LIST, label="x" * 500)
    assert len(cmd.label) == P.MAX_ALARM_LABEL_CHARS
