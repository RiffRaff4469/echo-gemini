"""Wire protocol shared by echo-server and EchoTerminal.apk.

ONE outbound WebSocket carries every channel (HANDOFF section 3). Two frame
kinds ride it:

  * **Text frames** are JSON control messages wrapped in a versioned envelope:
    ``{"v": 1, "t": "<type>", "ts": <epoch_ms>, ...fields}``.

  * **Binary frames** are media. They carry an 8-byte header so audio up,
    audio down and video up can share the socket without a JSON/base64 tax on
    every 20 ms of PCM::

        offset  size  field
        0       1     version (== PROTOCOL_VERSION)
        1       1     channel (Channel)
        2       2     flags   (uint16 big-endian, reserved -- must be 0)
        4       4     seq     (uint32 big-endian, per-channel, wraps)
        8       ...   payload

Any change here must be mirrored in ``EchoTerminal/.../Protocol.kt``. Bump
``PROTOCOL_VERSION`` when the shape changes incompatibly; the server rejects a
device whose ``hello`` announces a different major version.
"""

from __future__ import annotations

import json
import math
import struct
import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, ClassVar

PROTOCOL_VERSION = 1

# Additive changes inside v1 bump the MINOR only. v1.1 adds the alarm/timer
# channel (``alarm_command`` / ``alarm_state`` / ``alarm_fired``) and the
# ``now_playing`` display card. A v1.0 peer stays compatible: it simply never
# sends or understands the new types, and both sides ignore what they do not
# recognise. ``hello`` and ``welcome`` announce it so each end can log the skew.
PROTOCOL_MINOR = 1

# --- media formats ----------------------------------------------------------
# Gemini Live: audio in is PCM16 16 kHz mono LE, audio out is 24 kHz
# (HANDOFF section 13). The device produces and consumes exactly these, so the
# server passes both through without resampling.
AUDIO_UP_RATE = 16_000
AUDIO_DOWN_RATE = 24_000
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_WIDTH = 2  # PCM16
AUDIO_FRAME_MS = 20
AUDIO_UP_FRAME_SAMPLES = AUDIO_UP_RATE * AUDIO_FRAME_MS // 1000  # 320
AUDIO_UP_FRAME_BYTES = AUDIO_UP_FRAME_SAMPLES * AUDIO_SAMPLE_WIDTH  # 640

# Video in: JPEG, max 1 FPS, 768x768 recommended (HANDOFF section 2).
VIDEO_MAX_FPS = 1.0
VIDEO_EDGE = 768
VIDEO_MIME = "image/jpeg"

HEADER_FORMAT = ">BBHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 8
assert HEADER_SIZE == 8


class ProtocolError(ValueError):
    """Malformed frame or message. Always the peer's fault, never a crash."""


class Channel(IntEnum):
    """Binary frame channels."""

    AUDIO_UP = 0x01  # device mic  -> server (PCM16 16 kHz)
    AUDIO_DOWN = 0x02  # server     -> device (PCM16 24 kHz)
    VIDEO_UP = 0x03  # device cam  -> server (JPEG)


class MsgType(str, Enum):
    """JSON control message types."""

    # device -> server
    HELLO = "hello"
    PONG = "pong"
    TAP = "tap"
    CAMERA_STATUS = "camera_status"
    DEVICE_LOG = "device_log"
    ERROR = "error"
    ALARM_STATE = "alarm_state"  # v1.1
    ALARM_FIRED = "alarm_fired"  # v1.1

    # server -> device
    WELCOME = "welcome"
    PING = "ping"
    STATE = "state"
    MIC = "mic"
    INTERRUPT = "interrupt"
    DISPLAY = "display"
    DISPLAY_CLEAR = "display_clear"
    VIDEO = "video"
    ALARM_COMMAND = "alarm_command"  # v1.1


class UiState(str, Enum):
    """Server-driven device UI state (HANDOFF section 8.2)."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class CameraStatus(str, Enum):
    """What the device's single camera owner is currently doing."""

    RELEASED = "released"
    OPENING = "opening"
    STREAMING = "streaming"
    SHUTTER_CLOSED = "shutter_closed"  # physical privacy latch, frames near-black
    ERROR = "error"


class DisplayType(str, Enum):
    """``POST /display`` content types (HANDOFF section 8.1)."""

    TEXT = "text"
    HTML = "html"
    IMAGE = "image"
    TIMER = "timer"
    NOW_PLAYING = "now_playing"  # v1.1 -- rendered like any other card


class AlarmOp(str, Enum):
    """What an ``alarm_command`` asks the device's local scheduler to do.

    Every op is applied on-device. The server is a convenience, never a
    dependency: alarms and timers keep working with the PC switched off, which
    is the whole point of scheduling them locally (BUILD-BRIEF-3 section 1).
    """

    SET_ALARM = "set_alarm"
    SET_TIMER = "set_timer"
    CANCEL = "cancel"
    LIST = "list"


class AlarmKind(str, Enum):
    ALARM = "alarm"
    TIMER = "timer"


# Wire form for repeating-alarm days. Lowercase English abbreviations rather
# than integers because the two platforms disagree about which day is 0
# (Python's ``weekday()`` starts on Monday, ``java.util.Calendar`` on Sunday) and
# a silent off-by-one here means an alarm on the wrong morning.
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

MAX_TIMER_DURATION_S = 24 * 60 * 60
MAX_ALARM_LABEL_CHARS = 64


# ---------------------------------------------------------------------------
# Binary media frames
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaFrame:
    """A binary frame: 8-byte header + opaque payload."""

    channel: Channel
    payload: bytes
    seq: int = 0
    flags: int = 0

    def encode(self) -> bytes:
        return (
            struct.pack(
                HEADER_FORMAT,
                PROTOCOL_VERSION,
                int(self.channel),
                self.flags & 0xFFFF,
                self.seq & 0xFFFFFFFF,
            )
            + self.payload
        )

    @staticmethod
    def decode(raw: bytes) -> "MediaFrame":
        if len(raw) < HEADER_SIZE:
            raise ProtocolError(f"binary frame too short: {len(raw)} bytes")
        version, channel, flags, seq = struct.unpack(
            HEADER_FORMAT, raw[:HEADER_SIZE]
        )
        if version != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported frame version {version}")
        try:
            ch = Channel(channel)
        except ValueError as exc:
            raise ProtocolError(f"unknown channel 0x{channel:02x}") from exc
        return MediaFrame(
            channel=ch, payload=raw[HEADER_SIZE:], seq=seq, flags=flags
        )


class SeqCounter:
    """Per-channel monotonic sequence numbers that wrap at 2**32."""

    def __init__(self) -> None:
        self._counters: dict[Channel, int] = {}

    def next(self, channel: Channel) -> int:
        n = self._counters.get(channel, 0)
        self._counters[channel] = (n + 1) & 0xFFFFFFFF
        return n


# ---------------------------------------------------------------------------
# Display commands
# ---------------------------------------------------------------------------

MAX_DISPLAY_DURATION_S = 24 * 60 * 60
MAX_HTML_BYTES = 512 * 1024


@dataclass
class DisplayCommand:
    """``{type, payload, duration, priority}`` -- HANDOFF section 8.1.

    ``duration`` is seconds on screen; ``0`` means "until replaced or cleared".
    Higher ``priority`` wins when two commands overlap.
    """

    type: DisplayType
    payload: dict[str, Any]
    duration: float = 0.0
    priority: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "payload": self.payload,
            "duration": self.duration,
            "priority": self.priority,
        }

    @staticmethod
    def from_dict(data: Any) -> "DisplayCommand":
        """Validate untrusted JSON from ``POST /display``.

        Raises ``ProtocolError`` with a caller-facing message on any problem;
        the HTTP layer turns that straight into a 400.
        """
        if not isinstance(data, dict):
            raise ProtocolError("body must be a JSON object")

        raw_type = data.get("type")
        try:
            dtype = DisplayType(raw_type)
        except ValueError:
            allowed = ", ".join(t.value for t in DisplayType)
            raise ProtocolError(
                f"type must be one of: {allowed} (got {raw_type!r})"
            ) from None

        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise ProtocolError("payload must be a JSON object")

        duration = data.get("duration", 0)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise ProtocolError("duration must be a number of seconds")
        if duration < 0 or duration > MAX_DISPLAY_DURATION_S:
            raise ProtocolError(
                f"duration must be between 0 and {MAX_DISPLAY_DURATION_S} seconds"
            )

        priority = data.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ProtocolError("priority must be an integer")

        cmd = DisplayCommand(
            type=dtype,
            payload=payload,
            duration=float(duration),
            priority=priority,
        )
        cmd._validate_payload()
        return cmd

    def _validate_payload(self) -> None:
        p = self.payload
        if self.type is DisplayType.TEXT:
            _require_str(p, "text")
            _optional_str(p, "subtitle")
        elif self.type is DisplayType.HTML:
            html = _require_str(p, "html")
            if len(html.encode("utf-8")) > MAX_HTML_BYTES:
                raise ProtocolError(
                    f"payload.html exceeds {MAX_HTML_BYTES} bytes"
                )
        elif self.type is DisplayType.IMAGE:
            url = _require_str(p, "url")
            if not url.startswith(("http://", "https://", "data:image/")):
                raise ProtocolError(
                    "payload.url must be http(s):// or a data:image/ URI"
                )
        elif self.type is DisplayType.TIMER:
            _require_str(p, "label")
            seconds = p.get("seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                raise ProtocolError("payload.seconds must be a number")
            if seconds <= 0:
                raise ProtocolError("payload.seconds must be positive")
        elif self.type is DisplayType.NOW_PLAYING:
            # Plumbing only: this card is pushed by whatever ends up driving
            # playback (see docs/SPOTIFY.md). Nothing in this repo plays audio
            # from it -- it is a picture of what is playing, not a player.
            _require_str(p, "title")
            _optional_str(p, "artist")
            _optional_str(p, "album")
            art = _optional_str(p, "art_url")
            if art is not None and not art.startswith(
                ("http://", "https://", "data:image/")
            ):
                raise ProtocolError(
                    "payload.art_url must be http(s):// or a data:image/ URI"
                )
            progress = _optional_number(p, "progress_s")
            duration = _optional_number(p, "duration_s")
            if progress is not None and progress < 0:
                raise ProtocolError("payload.progress_s must not be negative")
            if duration is not None and duration <= 0:
                raise ProtocolError("payload.duration_s must be positive")
            is_playing = p.get("is_playing")
            if is_playing is not None and not isinstance(is_playing, bool):
                raise ProtocolError("payload.is_playing must be true or false")


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"payload.{key} must be a non-empty string")
    return value


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtocolError(f"payload.{key} must be a string when present")
    return value


def _optional_number(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"payload.{key} must be a number when present")
    return float(value)


# ---------------------------------------------------------------------------
# JSON control messages
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """Base for every JSON control message.

    Subclasses set ``TYPE`` and declare their fields; ``fields()`` returns the
    type-specific body that gets merged into the envelope.
    """

    TYPE: ClassVar[MsgType]

    ts: int = field(default=0, kw_only=True)

    def fields(self) -> dict[str, Any]:
        return {}

    def to_dict(self) -> dict[str, Any]:
        body = {
            "v": PROTOCOL_VERSION,
            "t": self.TYPE.value,
            "ts": self.ts or int(time.time() * 1000),
        }
        body.update(self.fields())
        return body

    def encode(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


# --- device -> server -------------------------------------------------------


@dataclass
class Hello(Message):
    TYPE: ClassVar[MsgType] = MsgType.HELLO

    device_id: str
    app_version: str = "unknown"
    protocol_version: int = PROTOCOL_VERSION
    protocol_minor: int = PROTOCOL_MINOR
    capabilities: dict[str, Any] = field(default_factory=dict)

    def fields(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "app_version": self.app_version,
            "protocol_version": self.protocol_version,
            "protocol_minor": self.protocol_minor,
            "capabilities": self.capabilities,
        }


@dataclass
class Pong(Message):
    TYPE: ClassVar[MsgType] = MsgType.PONG

    nonce: int = 0

    def fields(self) -> dict[str, Any]:
        return {"nonce": self.nonce}


@dataclass
class Tap(Message):
    """Tap-anywhere-to-talk. A permanent manual override, not a v1 shim
    (HANDOFF section 1) -- the mic is weak enough that wake word will miss."""

    TYPE: ClassVar[MsgType] = MsgType.TAP

    pressed: bool = True

    def fields(self) -> dict[str, Any]:
        return {"pressed": self.pressed}


@dataclass
class CameraStatusMsg(Message):
    TYPE: ClassVar[MsgType] = MsgType.CAMERA_STATUS

    status: CameraStatus
    detail: str = ""

    def fields(self) -> dict[str, Any]:
        return {"status": self.status.value, "detail": self.detail}


@dataclass
class DeviceLog(Message):
    TYPE: ClassVar[MsgType] = MsgType.DEVICE_LOG

    level: str = "info"
    message: str = ""

    def fields(self) -> dict[str, Any]:
        return {"level": self.level, "message": self.message}


@dataclass
class ErrorMsg(Message):
    TYPE: ClassVar[MsgType] = MsgType.ERROR

    code: str = "unknown"
    message: str = ""

    def fields(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


# --- server -> device -------------------------------------------------------


@dataclass
class Welcome(Message):
    TYPE: ClassVar[MsgType] = MsgType.WELCOME

    protocol_version: int = PROTOCOL_VERSION
    protocol_minor: int = PROTOCOL_MINOR
    heartbeat_interval_s: float = 15.0
    audio_up_rate: int = AUDIO_UP_RATE
    audio_down_rate: int = AUDIO_DOWN_RATE
    live_enabled: bool = False

    def fields(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "protocol_minor": self.protocol_minor,
            "heartbeat_interval_s": self.heartbeat_interval_s,
            "audio_up_rate": self.audio_up_rate,
            "audio_down_rate": self.audio_down_rate,
            "live_enabled": self.live_enabled,
        }


@dataclass
class Ping(Message):
    TYPE: ClassVar[MsgType] = MsgType.PING

    nonce: int = 0

    def fields(self) -> dict[str, Any]:
        return {"nonce": self.nonce}


@dataclass
class StateMsg(Message):
    TYPE: ClassVar[MsgType] = MsgType.STATE

    state: UiState = UiState.IDLE

    def fields(self) -> dict[str, Any]:
        return {"state": self.state.value}


@dataclass
class Mic(Message):
    """Uplink gate. Half-duplex: the server closes the mic while it is
    speaking rather than doing acoustic echo cancellation on one weak
    microphone (HANDOFF section 8.2)."""

    TYPE: ClassVar[MsgType] = MsgType.MIC

    enabled: bool = True
    gain: float | None = None

    def fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {"enabled": self.enabled}
        if self.gain is not None:
            out["gain"] = self.gain
        return out


@dataclass
class Interrupt(Message):
    """Barge-in: drop everything queued in AudioTrack right now."""

    TYPE: ClassVar[MsgType] = MsgType.INTERRUPT

    def fields(self) -> dict[str, Any]:
        return {}


@dataclass
class Display(Message):
    TYPE: ClassVar[MsgType] = MsgType.DISPLAY

    command: DisplayCommand = None  # type: ignore[assignment]

    def fields(self) -> dict[str, Any]:
        return self.command.to_dict()


@dataclass
class DisplayClear(Message):
    TYPE: ClassVar[MsgType] = MsgType.DISPLAY_CLEAR

    def fields(self) -> dict[str, Any]:
        return {}


@dataclass
class Video(Message):
    """Vision control. The camera opens ONLY when this arrives with
    ``enabled=True`` and is released on ``enabled=False`` (HANDOFF 8.3)."""

    TYPE: ClassVar[MsgType] = MsgType.VIDEO

    enabled: bool = False
    fps: float = VIDEO_MAX_FPS
    width: int = VIDEO_EDGE
    height: int = VIDEO_EDGE
    jpeg_quality: int = 80

    def __post_init__(self) -> None:
        # The API caps at 1 FPS; extra frames are wasted device work.
        self.fps = min(self.fps, VIDEO_MAX_FPS)

    def fields(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "jpeg_quality": self.jpeg_quality,
        }


# ---------------------------------------------------------------------------
# Alarms and timers (v1.1)
#
# The device owns the schedule. The server can ask it to change (``alarm_command``)
# and is told what happened (``alarm_state``, ``alarm_fired``), but it holds no
# authoritative copy and nothing here is required for an alarm to ring -- with
# the PC off the device still wakes the household on time.
# ---------------------------------------------------------------------------


@dataclass
class AlarmEntry:
    """One alarm or timer as the device currently holds it.

    Alarms carry ``time_epoch_ms`` (the next fire time) and, when repeating,
    ``days``. Timers carry ``remaining_s`` against the original ``duration_s``.
    """

    id: str
    kind: AlarmKind
    label: str = ""
    time_epoch_ms: int = 0
    days: list[str] = field(default_factory=list)
    enabled: bool = True
    duration_s: float = 0.0
    remaining_s: float = 0.0
    ringing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "label": self.label,
            "time_epoch_ms": self.time_epoch_ms,
            "days": list(self.days),
            "enabled": self.enabled,
            "duration_s": self.duration_s,
            "remaining_s": self.remaining_s,
            "ringing": self.ringing,
        }

    @staticmethod
    def from_dict(data: Any) -> "AlarmEntry":
        if not isinstance(data, dict):
            raise ProtocolError("alarm entry must be a JSON object")
        entry_id = data.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise ProtocolError("alarm entry needs a non-empty string id")
        try:
            kind = AlarmKind(data.get("kind"))
        except ValueError:
            raise ProtocolError(
                f"alarm entry kind must be alarm|timer (got {data.get('kind')!r})"
            ) from None
        return AlarmEntry(
            id=entry_id,
            kind=kind,
            label=str(data.get("label", "")),
            time_epoch_ms=int(data.get("time_epoch_ms", 0) or 0),
            days=_clean_days(data.get("days")),
            enabled=bool(data.get("enabled", True)),
            duration_s=float(data.get("duration_s", 0) or 0),
            remaining_s=float(data.get("remaining_s", 0) or 0),
            ringing=bool(data.get("ringing", False)),
        )


def _clean_days(raw: Any) -> list[str]:
    """Normalise a days list, rejecting anything not in ``DAY_NAMES``.

    Order is normalised to Monday-first so ``["sun","mon"]`` and ``["mon","sun"]``
    compare equal and read the same way back to the user.
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ProtocolError("days must be a list of mon|tue|wed|thu|fri|sat|sun")
    seen = set()
    for item in raw:
        if not isinstance(item, str) or item.lower() not in DAY_NAMES:
            raise ProtocolError(
                f"days entries must be one of {'|'.join(DAY_NAMES)} (got {item!r})"
            )
        seen.add(item.lower())
    return [day for day in DAY_NAMES if day in seen]


@dataclass
class AlarmCommand(Message):
    """server -> device: change the local schedule.

    ``req_id`` is not in the brief's field list but is what makes the Gemini
    tool handler correct: it waits for the ``alarm_state`` carrying the same
    ``req_id`` rather than for whichever state happens to arrive next, so an
    unsolicited push (a timer ticking down) cannot be mistaken for the ack.
    """

    TYPE: ClassVar[MsgType] = MsgType.ALARM_COMMAND

    op: AlarmOp = AlarmOp.LIST
    id: str = ""
    label: str = ""
    time_epoch_ms: int = 0
    days: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    # cancel only: narrow the target to one kind. Empty means "either".
    kind: str = ""
    req_id: str = ""

    def __post_init__(self) -> None:
        self.days = _clean_days(self.days)
        self.label = self.label[:MAX_ALARM_LABEL_CHARS]
        if self.op is AlarmOp.SET_ALARM and self.time_epoch_ms <= 0:
            raise ProtocolError("set_alarm needs a positive time_epoch_ms")
        if self.op is AlarmOp.SET_TIMER:
            if not math.isfinite(self.duration_s) or self.duration_s < 0.001:
                raise ProtocolError("set_timer needs a positive duration_s")
            if self.duration_s > MAX_TIMER_DURATION_S:
                raise ProtocolError(
                    f"set_timer duration_s must be <= {MAX_TIMER_DURATION_S}"
                )
        if self.kind and self.kind not in (k.value for k in AlarmKind):
            raise ProtocolError("kind must be alarm|timer when present")

    def fields(self) -> dict[str, Any]:
        return {
            "op": self.op.value,
            "id": self.id,
            "label": self.label,
            "time_epoch_ms": self.time_epoch_ms,
            "days": list(self.days),
            "duration_s": self.duration_s,
            "kind": self.kind,
            "req_id": self.req_id,
        }


@dataclass
class AlarmStateMsg(Message):
    """device -> server: the whole schedule, after any change and on request.

    Sent as a full snapshot rather than a delta. There is one device and at most
    a handful of entries, so re-sending everything removes a class of drift bugs
    for a few hundred bytes.
    """

    TYPE: ClassVar[MsgType] = MsgType.ALARM_STATE

    alarms: list[AlarmEntry] = field(default_factory=list)
    timers: list[AlarmEntry] = field(default_factory=list)
    req_id: str = ""
    # False when Android denies SCHEDULE_EXACT_ALARM: the device will still
    # ring, but possibly late, and the model should say so rather than promise
    # an exact time it cannot keep.
    exact_allowed: bool = True
    error: str = ""

    def fields(self) -> dict[str, Any]:
        return {
            "alarms": [a.to_dict() for a in self.alarms],
            "timers": [t.to_dict() for t in self.timers],
            "req_id": self.req_id,
            "exact_allowed": self.exact_allowed,
            "error": self.error,
        }


@dataclass
class AlarmFired(Message):
    """device -> server: something just went off, and is ringing locally.

    Purely informational. The chime is already playing on the device by the
    time this is sent -- it is what lets Gemini say "your timer is up", not what
    makes the noise.
    """

    TYPE: ClassVar[MsgType] = MsgType.ALARM_FIRED

    kind: AlarmKind = AlarmKind.ALARM
    id: str = ""
    label: str = ""

    def fields(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "id": self.id, "label": self.label}


_DECODERS: dict[str, Any] = {}


def _register(cls: type[Message]) -> None:
    _DECODERS[cls.TYPE.value] = cls


for _cls in (
    Hello,
    Pong,
    Tap,
    CameraStatusMsg,
    DeviceLog,
    ErrorMsg,
    Welcome,
    Ping,
    StateMsg,
    Mic,
    Interrupt,
    Display,
    DisplayClear,
    Video,
    AlarmCommand,
    AlarmStateMsg,
    AlarmFired,
):
    _register(_cls)


def decode(raw: str | bytes) -> Message:
    """Parse a JSON control frame. Raises ``ProtocolError`` on anything odd."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("control message must be a JSON object")

    version = data.get("v")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {version!r}")

    mtype = data.get("t")
    cls = _DECODERS.get(mtype)
    if cls is None:
        raise ProtocolError(f"unknown message type {mtype!r}")

    ts = data.get("ts", 0)
    if not isinstance(ts, int):
        ts = 0

    try:
        return _build(cls, data, ts)
    except ProtocolError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError(f"malformed {mtype} message: {exc}") from exc


def _build(cls: type[Message], data: dict[str, Any], ts: int) -> Message:
    if cls is Hello:
        device_id = data.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            raise ProtocolError("hello.device_id must be a non-empty string")
        return Hello(
            device_id=device_id,
            app_version=str(data.get("app_version", "unknown")),
            protocol_version=int(data.get("protocol_version", PROTOCOL_VERSION)),
            # A v1.0 client omits this entirely; absence means 0, not an error.
            protocol_minor=int(data.get("protocol_minor", 0)),
            capabilities=data.get("capabilities") or {},
            ts=ts,
        )
    if cls is Pong:
        return Pong(nonce=int(data.get("nonce", 0)), ts=ts)
    if cls is Tap:
        return Tap(pressed=bool(data.get("pressed", True)), ts=ts)
    if cls is CameraStatusMsg:
        return CameraStatusMsg(
            status=CameraStatus(data.get("status")),
            detail=str(data.get("detail", "")),
            ts=ts,
        )
    if cls is DeviceLog:
        return DeviceLog(
            level=str(data.get("level", "info")),
            message=str(data.get("message", "")),
            ts=ts,
        )
    if cls is ErrorMsg:
        return ErrorMsg(
            code=str(data.get("code", "unknown")),
            message=str(data.get("message", "")),
            ts=ts,
        )
    if cls is Welcome:
        return Welcome(
            protocol_version=int(data.get("protocol_version", PROTOCOL_VERSION)),
            protocol_minor=int(data.get("protocol_minor", 0)),
            heartbeat_interval_s=float(data.get("heartbeat_interval_s", 15.0)),
            audio_up_rate=int(data.get("audio_up_rate", AUDIO_UP_RATE)),
            audio_down_rate=int(data.get("audio_down_rate", AUDIO_DOWN_RATE)),
            live_enabled=bool(data.get("live_enabled", False)),
            ts=ts,
        )
    if cls is Ping:
        return Ping(nonce=int(data.get("nonce", 0)), ts=ts)
    if cls is StateMsg:
        return StateMsg(state=UiState(data.get("state")), ts=ts)
    if cls is Mic:
        gain = data.get("gain")
        return Mic(
            enabled=bool(data.get("enabled", True)),
            gain=float(gain) if gain is not None else None,
            ts=ts,
        )
    if cls is Interrupt:
        return Interrupt(ts=ts)
    if cls is Display:
        return Display(command=DisplayCommand.from_dict(data), ts=ts)
    if cls is DisplayClear:
        return DisplayClear(ts=ts)
    if cls is Video:
        return Video(
            enabled=bool(data.get("enabled", False)),
            fps=float(data.get("fps", VIDEO_MAX_FPS)),
            width=int(data.get("width", VIDEO_EDGE)),
            height=int(data.get("height", VIDEO_EDGE)),
            jpeg_quality=int(data.get("jpeg_quality", 80)),
            ts=ts,
        )
    if cls is AlarmCommand:
        try:
            op = AlarmOp(data.get("op"))
        except ValueError:
            allowed = "|".join(o.value for o in AlarmOp)
            raise ProtocolError(
                f"alarm_command.op must be one of {allowed} (got {data.get('op')!r})"
            ) from None
        return AlarmCommand(
            op=op,
            id=str(data.get("id", "")),
            label=str(data.get("label", "")),
            time_epoch_ms=int(data.get("time_epoch_ms", 0) or 0),
            days=data.get("days"),
            duration_s=float(data.get("duration_s", 0) or 0),
            kind=str(data.get("kind", "")),
            req_id=str(data.get("req_id", "")),
            ts=ts,
        )
    if cls is AlarmStateMsg:
        return AlarmStateMsg(
            alarms=[AlarmEntry.from_dict(a) for a in data.get("alarms") or []],
            timers=[AlarmEntry.from_dict(t) for t in data.get("timers") or []],
            req_id=str(data.get("req_id", "")),
            exact_allowed=bool(data.get("exact_allowed", True)),
            error=str(data.get("error", "")),
            ts=ts,
        )
    if cls is AlarmFired:
        try:
            kind = AlarmKind(data.get("kind"))
        except ValueError:
            raise ProtocolError(
                f"alarm_fired.kind must be alarm|timer (got {data.get('kind')!r})"
            ) from None
        return AlarmFired(
            kind=kind,
            id=str(data.get("id", "")),
            label=str(data.get("label", "")),
            ts=ts,
        )
    raise ProtocolError(f"no decoder wired for {cls.__name__}")
