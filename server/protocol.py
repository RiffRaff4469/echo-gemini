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
# ``now_playing`` display card; v1.2 adds ``weather``, pushed for the ambient
# home screen; v1.3 adds the post-answer quiet window (``session_quiet`` down,
# ``stay`` up); v1.4 replaces ``stay`` with ``stop`` -- a tap during a session
# now ENDS it rather than extending it (FIX-BRIEF-11); v1.5 adds music --
# ``Channel.AUDIO_MUSIC`` down and ``media_control`` up (SPOTIFY-BRIEF-12). A
# v1.0 peer stays compatible: it simply never sends or understands the new
# types, and both sides ignore what they do not recognise. ``hello`` and
# ``welcome`` announce it so each end can log the skew.
PROTOCOL_MINOR = 6

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
    AUDIO_MUSIC = 0x04  # server   -> device (PCM16 24 kHz), v1.5


# Why music does not simply reuse ``AUDIO_DOWN``, which carries the same format
# to the same speaker: on the device, ``AUDIO_DOWN`` feeds ``AudioPlayback``,
# and ``AudioPlayback`` means *the assistant is talking*. It reports speaking
# state, which closes the microphone uplink (half-duplex, HANDOFF 8.2), and
# barge-in flushes it. Music on that channel would therefore hold the mic shut
# for as long as it played -- no wake word, so no way to say "pause" -- and the
# first thing the model said would silence the album.
#
# So music gets its own channel and its own ``AudioTrack`` on the device, tagged
# USAGE_MEDIA rather than USAGE_ASSISTANT: two streams, mixed by Android, each
# with the lifecycle it actually wants. Arbitration between them stays a server
# decision (``spotify.SpotifyController`` holds), not a mixing one.


class MsgType(str, Enum):
    """JSON control message types."""

    # device -> server
    HELLO = "hello"
    PONG = "pong"
    TAP = "tap"
    STOP = "stop"  # v1.4
    STAY = "stay"  # v1.3, superseded by STOP -- decoded, then treated as one
    BUTTON = "button"  # v1.6: physical mic button, classified by the device
    CAMERA_STATUS = "camera_status"
    DEVICE_LOG = "device_log"
    ERROR = "error"
    ALARM_STATE = "alarm_state"  # v1.1
    ALARM_FIRED = "alarm_fired"  # v1.1
    STOPWATCH_STATE = "stopwatch_state"  # v1.6, device -> server
    MEDIA_CONTROL = "media_control"  # v1.5
    SELECT = "select"  # v1.6: tap on an options-panel row (UI-BRIEF-16)

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
    WEATHER = "weather"  # v1.2
    SESSION_QUIET = "session_quiet"  # v1.3
    MUTE = "mute"  # v1.6: voice-initiated mute (server -> device)
    LAYOUT = "layout"  # v1.6: focus-driven layout engine (UI-BRIEF-14)


class UiState(str, Enum):
    """Server-driven device UI state (HANDOFF section 8.2)."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class LayoutFocus(str, Enum):
    """Which layout template the home screen should show (UI-BRIEF-14)."""

    HOME = "home"
    MUSIC = "music"
    CHAT = "chat"


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
    # v1.6 -- interactive panels (UI-BRIEF-16). Rendered as TRUSTED DOM by the
    # device's own renderer (same trust class as the other cards), never as
    # server HTML. OPTIONS rows are tappable and come back as `select`.
    OPTIONS = "options"
    LIST = "list"


# Panel payload bounds (UI-BRIEF-16). OPTIONS rows must fit the 960x480 touch
# surface with >= 48 px tap targets, so the count is hard-capped and labels are
# clipped at the server. LIST is an enumerated read-only panel (steps, recipes,
# schedules) with a looser cap.
OPTIONS_MIN = 2
OPTIONS_MAX = 6
OPTION_LABEL_MAX_CHARS = 28
OPTION_SUB_MAX_CHARS = 40
OPTION_HEADER_MAX_CHARS = 80
LIST_MIN_ITEMS = 1
LIST_MAX_ITEMS = 15
LIST_ITEM_MAX_CHARS = 64
LIST_HEADER_MAX_CHARS = 64


class MediaAction(str, Enum):
    """What the touch row on the now-playing card is asking for (v1.5).

    Deliberately transport-only. Volume is not here: the panel has no volume
    affordance, the account's volume is a Connect property shared with every
    other client, and "turn it down" is a sentence the model already handles.
    """

    TOGGLE = "toggle"  # pause if playing, resume if not
    PAUSE = "pause"
    RESUME = "resume"
    NEXT = "next"
    PREVIOUS = "previous"


class AlarmOp(str, Enum):
    """What an ``alarm_command`` asks the device's local scheduler to do.

    Every op is applied on-device. The server is a convenience, never a
    dependency: alarms and timers keep working with the PC switched off, which
    is the whole point of scheduling them locally (BUILD-BRIEF-3 section 1).
    The stopwatch ops (v1.6, CLOCK-BRIEF-STOPWATCH) ride the same channel: the
    device owns the running clock, the server only relays voice intents.
    """

    SET_ALARM = "set_alarm"
    SET_TIMER = "set_timer"
    CANCEL = "cancel"
    LIST = "list"
    STOPWATCH_START = "stopwatch_start"
    STOPWATCH_PAUSE = "stopwatch_pause"
    STOPWATCH_RESET = "stopwatch_reset"
    STOPWATCH_LAP = "stopwatch_lap"
    STOPWATCH_STATUS = "stopwatch_status"


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
            # Pushed by ``spotify.SpotifyController`` while music is playing on
            # this device, and still a plain display card: it is a picture of
            # what is playing, not the player. The transport buttons the ambient
            # page draws on it come back as ``media_control``, not as anything
            # in this payload.
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
        elif self.type is DisplayType.OPTIONS:
            # Tappable choice rows (UI-BRIEF-16). Count is bounded so the rows
            # keep >= 48 px touch targets on the 960x480 panel; anything longer
            # than the caps below is a caller bug, not something to render.
            _optional_str(p, "title")
            _optional_str(p, "question")
            raw = p.get("options")
            if not isinstance(raw, list) or not (
                OPTIONS_MIN <= len(raw) <= OPTIONS_MAX
            ):
                raise ProtocolError(
                    f"payload.options must be a list of {OPTIONS_MIN}-"
                    f"{OPTIONS_MAX} items (got "
                    f"{'not a list' if not isinstance(raw, list) else len(raw)})"
                )
            for i, entry in enumerate(raw):
                if not isinstance(entry, dict):
                    raise ProtocolError(
                        f"payload.options[{i}] must be an object with a label"
                    )
                label = entry.get("label")
                if not isinstance(label, str) or not label:
                    raise ProtocolError(
                        f"payload.options[{i}].label must be a non-empty string"
                    )
                if len(label) > OPTION_LABEL_MAX_CHARS:
                    raise ProtocolError(
                        f"payload.options[{i}].label is longer than "
                        f"{OPTION_LABEL_MAX_CHARS} characters"
                    )
                sub = entry.get("sub")
                if sub is not None:
                    if not isinstance(sub, str):
                        raise ProtocolError(
                            f"payload.options[{i}].sub must be a string when present"
                        )
                    if len(sub) > OPTION_SUB_MAX_CHARS:
                        raise ProtocolError(
                            f"payload.options[{i}].sub is longer than "
                            f"{OPTION_SUB_MAX_CHARS} characters"
                        )
        elif self.type is DisplayType.LIST:
            # Read-only enumerated panel (UI-BRIEF-16): steps, recipes, lists.
            _optional_str(p, "title")
            raw = p.get("items")
            if not isinstance(raw, list) or not (
                LIST_MIN_ITEMS <= len(raw) <= LIST_MAX_ITEMS
            ):
                raise ProtocolError(
                    f"payload.items must be a list of {LIST_MIN_ITEMS}-"
                    f"{LIST_MAX_ITEMS} strings (got "
                    f"{'not a list' if not isinstance(raw, list) else len(raw)})"
                )
            for i, item in enumerate(raw):
                if not isinstance(item, str) or not item:
                    raise ProtocolError(
                        f"payload.items[{i}] must be a non-empty string"
                    )
                if len(item) > LIST_ITEM_MAX_CHARS:
                    raise ProtocolError(
                        f"payload.items[{i}] is longer than "
                        f"{LIST_ITEM_MAX_CHARS} characters"
                    )


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


def clip_text(text: str, max_chars: int) -> str:
    """Clip a panel string for the 960x480 screen (UI-BRIEF-16).

    The tools that build ``options``/``list`` panels clip long labels here so
    a row always fits; the *panel state* keeps the untruncated original so a
    tap maps back to exactly what the model wrote. One trailing ellipsis marks
    the clip.
    """
    if max_chars <= 1 or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "\u2026"


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
class Stop(Message):
    """Tap-to-stop: end the session that is already open (v1.4).

    Deliberately a separate type from ``tap`` rather than a flag on it. A tap
    that OPENS a session and a tap that ENDS one are opposite intents, and the
    device is the only end that knows which it meant -- it can see whether the
    screen is showing a conversation. Carries no fields: the only thing to say
    is "now".

    This replaces v1.3's ``stay``, which bought the session another half minute.
    Using it made the owner's mental model plain: a hand going to the screen
    mid-answer means *enough*, not *carry on*. Cutting an answer off is the
    point, not a side effect.
    """

    TYPE: ClassVar[MsgType] = MsgType.STOP

    def fields(self) -> dict[str, Any]:
        return {}


@dataclass
class Stay(Message):
    """v1.3's tap-to-stay. Kept only so a device that has not been reflashed
    still says something the server can act on -- see ``Hub._on_stop``, which
    treats it exactly as ``stop``. Owner semantics supersede the old ones, so
    an old client's tap ends the conversation rather than doing nothing.
    """

    TYPE: ClassVar[MsgType] = MsgType.STAY

    def fields(self) -> dict[str, Any]:
        return {}


@dataclass
class Button(Message):
    """Physical mic button press, classified by the device (v1.6).

    The device is the only end that can time the press, so it measures the
    duration and sends the *semantic* action. ``talk_toggle`` is a short press
    (< 700 ms): start a session when idle, end it when one is active, unmute
    and start when muted. ``mute`` is a hold (>= 1 s): flip the privacy mute.
    The server is deliberately not in the key-timing loop.
    """

    TYPE: ClassVar[MsgType] = MsgType.BUTTON

    action: str = "talk_toggle"  # "talk_toggle" | "mute"

    def fields(self) -> dict[str, Any]:
        return {"action": self.action}


@dataclass
class Mute(Message):
    """Voice-initiated privacy mute (server -> device, v1.6).

    Carries the full state, never a toggle: ``on`` is what the server wants
    the device to end up in. The device owns the mute state (it works with
    the PC off), so this asks it to apply the transition -- the device's
    reply arrives as a ``button mute`` action on the next state flip.
    """

    TYPE: ClassVar[MsgType] = MsgType.MUTE

    on: bool = True

    def fields(self) -> dict[str, Any]:
        return {"on": self.on}


@dataclass
class MediaControl(Message):
    """device -> server: the user touched a transport control (v1.5).

    The card is drawn by the ambient page, but the player is on the PC, so the
    button cannot do anything locally -- it says what was pressed and the server
    turns that into a Spotify call. Nothing on the device is optimistic about
    it: the button state changes when the next ``now_playing`` push says it did,
    which is the only thing that knows.

    A press that arrives with Spotify disabled is answered with an ``error``
    message and otherwise ignored; it cannot start anything.
    """

    TYPE: ClassVar[MsgType] = MsgType.MEDIA_CONTROL

    action: MediaAction = MediaAction.TOGGLE

    def fields(self) -> dict[str, Any]:
        return {"action": self.action.value}


@dataclass
class Select(Message):
    """device -> server: the user tapped option row ``index`` (0-based) of the
    options panel currently on screen (UI-BRIEF-16, v1.6).

    Deliberately NOT a ``tap`` or ``button`` message: since v1.6 the screen is
    UI-only and talk is owned by the mic button and the wake word. This says
    which *label* the user picked; the server maps it back to the stored panel
    so the model hears "the user chose <label>", never a bare index.
    """

    TYPE: ClassVar[MsgType] = MsgType.SELECT

    index: int = 0

    def fields(self) -> dict[str, Any]:
        return {"index": self.index}


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


@dataclass
class WeatherMsg(Message):
    """server -> device: current conditions for the ambient home screen (v1.2).

    The device has no internet of its own, so this is the only way it can know
    the weather (see ``server/weather.py``). Three fields and no more: the
    temperature in Celsius, the WMO weather code the client maps to one of its
    hand-drawn glyphs, and whether it is daytime *at the observed location*,
    which is not the same question as what the device's own clock says.

    Purely decorative. Losing it costs a placeholder on one card and nothing
    else -- the clock, the link and the voice path do not read it.
    """

    TYPE: ClassVar[MsgType] = MsgType.WEATHER

    temp_c: float = 0.0
    code: int = 0
    is_day: bool = True

    def fields(self) -> dict[str, Any]:
        return {"temp_c": self.temp_c, "code": self.code, "is_day": self.is_day}


@dataclass
class SessionQuiet(Message):
    """server -> device: the post-answer quiet window is running (v1.3).

    The model has finished answering and the session will close itself in
    ``closes_in_s`` seconds unless someone speaks or taps. The device needs the
    deadline -- not just the fact -- because the affordance it draws is a hint
    that fades in over the last few seconds; without a number it would have to
    guess the server's ``POST_ANSWER_SILENCE_S``.

    Re-sent whenever the deadline moves (a tap extends it) and once with
    ``active=False`` when the window is cancelled or the session ends. Purely an
    affordance: a device that ignores it simply gets no warning before the
    session goes, which is what today's behaviour already is.
    """

    TYPE: ClassVar[MsgType] = MsgType.SESSION_QUIET

    active: bool = False
    closes_in_s: float = 0.0

    def fields(self) -> dict[str, Any]:
        return {"active": self.active, "closes_in_s": self.closes_in_s}


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


@dataclass
class StopwatchStateMsg(Message):
    """device -> server: the stopwatch, on every transition (v1.6).

    Pushed on start/pause/reset/lap -- never ticked every second. The device
    owns the running clock; this is the mirror the voice tools answer from.
    """

    TYPE: ClassVar[MsgType] = MsgType.STOPWATCH_STATE

    running: bool = False
    elapsed_ms: int = 0
    laps_ms: list[int] = field(default_factory=list)
    req_id: str = ""

    def fields(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "elapsed_ms": self.elapsed_ms,
            "laps_ms": self.laps_ms,
            "req_id": self.req_id,
        }


@dataclass
class LayoutMsg(Message):
    """server -> device: which layout template to show (UI-BRIEF-14, v1.6).

    Carries the full focus, never a delta: ``home`` | ``music`` | ``chat``.
    The device rearranges its single DOM around the template; a missing or
    unknown focus falls back to ``home``.
    """

    TYPE: ClassVar[MsgType] = MsgType.LAYOUT

    focus: LayoutFocus = LayoutFocus.HOME

    def fields(self) -> dict[str, Any]:
        return {"focus": self.focus.value}


_DECODERS: dict[str, Any] = {}


def _register(cls: type[Message]) -> None:
    _DECODERS[cls.TYPE.value] = cls


for _cls in (
    Hello,
    Pong,
    Tap,
    Stop,
    Stay,
    MediaControl,
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
    WeatherMsg,
    SessionQuiet,
    AlarmCommand,
    AlarmStateMsg,
    AlarmFired,
    StopwatchStateMsg,
    Button,
    Mute,
    LayoutMsg,
    Select,
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
    if cls is Stop:
        return Stop(ts=ts)
    if cls is Stay:
        return Stay(ts=ts)
    if cls is Button:
        action = str(data.get("action", "talk_toggle"))
        if action not in ("talk_toggle", "mute"):
            raise ProtocolError(
                f"button.action must be talk_toggle or mute (got {action!r})"
            )
        return Button(action=action, ts=ts)
    if cls is Select:
        index = data.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ProtocolError("select.index must be an integer >= 0")
        return Select(index=index, ts=ts)
    if cls is Mute:
        return Mute(on=bool(data.get("on", True)), ts=ts)
    if cls is MediaControl:
        try:
            action = MediaAction(data.get("action"))
        except ValueError:
            allowed = "|".join(a.value for a in MediaAction)
            raise ProtocolError(
                f"media_control.action must be one of {allowed} "
                f"(got {data.get('action')!r})"
            ) from None
        return MediaControl(action=action, ts=ts)
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
    if cls is WeatherMsg:
        temp = data.get("temp_c")
        if isinstance(temp, bool) or not isinstance(temp, (int, float)):
            raise ProtocolError("weather.temp_c must be a number")
        if not math.isfinite(float(temp)):
            raise ProtocolError("weather.temp_c must be finite")
        code = data.get("code", 0)
        if isinstance(code, bool) or not isinstance(code, int):
            raise ProtocolError("weather.code must be an integer WMO code")
        return WeatherMsg(
            temp_c=float(temp),
            code=code,
            is_day=bool(data.get("is_day", True)),
            ts=ts,
        )
    if cls is SessionQuiet:
        closes_in = data.get("closes_in_s", 0)
        if isinstance(closes_in, bool) or not isinstance(closes_in, (int, float)):
            raise ProtocolError("session_quiet.closes_in_s must be a number")
        if not math.isfinite(float(closes_in)) or closes_in < 0:
            raise ProtocolError("session_quiet.closes_in_s must be finite and >= 0")
        return SessionQuiet(
            active=bool(data.get("active", False)),
            closes_in_s=float(closes_in),
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
    if cls is StopwatchStateMsg:
        laps = data.get("laps_ms", [])
        if not isinstance(laps, list) or not all(
            isinstance(x, int) and x >= 0 for x in laps
        ):
            raise ProtocolError("stopwatch_state.laps_ms must be a list of ms")
        elapsed = data.get("elapsed_ms", 0)
        if isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0:
            raise ProtocolError("stopwatch_state.elapsed_ms must be ms >= 0")
        return StopwatchStateMsg(
            running=bool(data.get("running", False)),
            elapsed_ms=elapsed,
            laps_ms=[int(x) for x in laps],
            req_id=str(data.get("req_id", "")),
            ts=ts,
        )
    if cls is LayoutMsg:
        try:
            focus = LayoutFocus(data.get("focus", "home"))
        except ValueError:
            focus = LayoutFocus.HOME
        return LayoutMsg(focus=focus, ts=ts)
    raise ProtocolError(f"no decoder wired for {cls.__name__}")
