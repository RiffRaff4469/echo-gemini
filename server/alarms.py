"""Server-side half of the alarm/timer feature.

Three jobs, none of which is "keep the schedule":

  1. Turn what a model says ("tomorrow at seven", "twenty minutes") into an
     absolute epoch or a duration, using this machine's clock.
  2. Send an ``alarm_command`` at the device and wait for the ``alarm_state``
     that acknowledges it, so a tool call can answer with what the device
     actually did rather than what it was asked to do.
  3. Render a state snapshot into wording a voice model can read out.

The device owns the schedule (``EchoTerminal/.../AlarmScheduler.kt``). Nothing
in this module is required for an alarm to ring: with the PC off the device
still fires its own ``AlarmManager`` and plays its own chime. That is the
product requirement, and it is why the server deliberately keeps no
authoritative copy it could disagree with.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import uuid
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any, Callable

from protocol import (
    DAY_NAMES,
    MAX_TIMER_DURATION_S,
    AlarmCommand,
    AlarmEntry,
    AlarmKind,
    AlarmOp,
    AlarmStateMsg,
    StopwatchStateMsg,
)

log = logging.getLogger("echo.alarms")

# How long a tool call waits for the device to acknowledge. Deliberately short:
# the model is mid-sentence with a person standing in front of it, and "I could
# not reach the display" beats ten seconds of silence.
DEFAULT_ACK_TIMEOUT_S = 3.0


class AlarmError(RuntimeError):
    """Anything the model should hear about as a plain sentence."""


class DeviceOffline(AlarmError):
    pass


class AckTimeout(AlarmError):
    pass


# ---------------------------------------------------------------------------
# Natural time -> epoch
# ---------------------------------------------------------------------------

_CLOCK_RE = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*$", re.IGNORECASE
)


def parse_clock(text: str) -> tuple[int, int]:
    """``"7"``, ``"7:05"``, ``"7:05 pm"``, ``"19:05"`` -> ``(hour, minute)``.

    Both 12- and 24-hour forms are accepted because the model produces both and
    which one it picks is not worth a round trip to correct. ``12 am`` is
    midnight and ``12 pm`` is noon, which is the one case people and naive
    modulo arithmetic disagree about.
    """
    match = _CLOCK_RE.match(text or "")
    if not match:
        raise AlarmError(f"could not read {text!r} as a time of day")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower().replace(".", "")

    if meridiem:
        if not 1 <= hour <= 12:
            raise AlarmError(f"{text!r} is not a valid 12-hour time")
        if meridiem == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise AlarmError(f"{text!r} is not a valid time of day")
    return hour, minute


def resolve_alarm_time(
    when: str,
    *,
    date: str = "",
    days: list[str] | None = None,
    now: datetime | None = None,
) -> int:
    """Resolve an alarm to an absolute epoch-ms in the server's local zone.

    ``when`` is either a bare time of day (``"7:05 am"``) or a full ISO-8601
    local datetime (``"2026-09-07T07:05"``). The rules for a bare time:

      * an explicit ``date`` pins it to that day;
      * otherwise, with ``days`` set, it is the next of those weekdays that has
        not already passed today;
      * otherwise it is today if that time is still ahead, and tomorrow if not.

    "Today if it is still ahead" is the rule people actually mean by "set an
    alarm for ten" at nine in the morning, and the rule they emphatically do not
    mean at eleven at night.
    """
    now = now or datetime.now()
    when = when.strip()
    relative = re.fullmatch(r"(today|tomorrow)(?:\s+at)?\s+(.+)", when, re.IGNORECASE)
    if relative:
        if date or days:
            raise AlarmError("Use a relative day or a date/repeat schedule, not both")
        date = (now.date() + timedelta(days=relative.group(1).lower() == "tomorrow")).isoformat()
        when = relative.group(2)
    if date and days:
        raise AlarmError("Use an explicit date or repeating weekdays, not both")

    if "t" in (when or "").lower() and any(ch == "-" for ch in when):
        # An ISO datetime the model resolved itself. Trust it, but still check
        # it is in the future -- a model that gets the date wrong should produce
        # an error the user hears, not an alarm that never rings.
        try:
            parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AlarmError(f"could not read {when!r} as a date and time") from exc
        if date or days:
            raise AlarmError("An ISO datetime cannot also specify a date or repeating days")
        if parsed.timestamp() <= now.timestamp():
            # No %-d / %#d here: those directives are platform-specific and this
            # server runs on Windows.
            raise AlarmError(
                f"{parsed:%A %d %B} at "
                f"{_clock_words(parsed.hour, parsed.minute)} is in the past"
            )
        return int(parsed.timestamp() * 1000)

    hour, minute = parse_clock(when)
    days = days or []

    if date:
        try:
            day = date_cls.fromisoformat(date.strip())
        except ValueError as exc:
            raise AlarmError(f"could not read {date!r} as a YYYY-MM-DD date") from exc
        target = datetime.combine(day, datetime.min.time()).replace(
            hour=hour, minute=minute
        )
        if target <= now:
            raise AlarmError(f"{target:%A} at {_clock_words(hour, minute)} is in the past")
        return int(target.timestamp() * 1000)

    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if days:
        wanted = {DAY_NAMES.index(d) for d in days}
        for offset in range(8):
            candidate = today + timedelta(days=offset)
            if candidate.weekday() in wanted and candidate > now:
                return int(candidate.timestamp() * 1000)
        raise AlarmError("no matching weekday found")  # unreachable with valid days

    if today <= now:
        today += timedelta(days=1)
    return int(today.timestamp() * 1000)


def resolve_duration(
    duration_s: float | None = None,
    *,
    hours: float = 0,
    minutes: float = 0,
    seconds: float = 0,
) -> float:
    """Collapse whatever combination of units the model used into seconds."""
    total = float(duration_s or 0) + hours * 3600 + minutes * 60 + seconds
    if not math.isfinite(total) or total < 0.001:
        raise AlarmError("a timer needs a positive duration")
    if total > MAX_TIMER_DURATION_S:
        raise AlarmError("timers are capped at 24 hours; use an alarm instead")
    return round(total, 3)


def normalise_days(raw: Any) -> list[str]:
    """Accept ``["monday","Tue"]``/``"weekdays"``/``"daily"`` -> wire day names.

    The model will say "every weekday" as often as it says a list, and rejecting
    that would push the failure into the conversation for no reason.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise AlarmError("Repeat days must be a list of weekday names")
    out: set[str] = set()
    for item in raw:
        token = str(item).strip().lower()
        if token in ("daily", "every day", "everyday", "all"):
            out.update(DAY_NAMES)
        elif token in ("weekday", "weekdays"):
            out.update(DAY_NAMES[:5])
        elif token in ("weekend", "weekends"):
            out.update(DAY_NAMES[5:])
        elif token in DAY_NAMES or token in {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}:
            out.add(token[:3])
        else:
            raise AlarmError(f"{item!r} is not a day of the week")
    return [d for d in DAY_NAMES if d in out]


# ---------------------------------------------------------------------------
# Rendering state back into words
# ---------------------------------------------------------------------------

_FULL_DAY = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}


def _clock_words(hour: int, minute: int) -> str:
    suffix = "am" if hour < 12 else "pm"
    display = hour % 12 or 12
    return f"{display}:{minute:02d} {suffix}"


def describe_entry(entry: AlarmEntry, now: datetime | None = None) -> str:
    """One human sentence fragment per entry, for the model to read out."""
    now = now or datetime.now()
    label = entry.label or ("Timer" if entry.kind is AlarmKind.TIMER else "Alarm")
    if entry.ringing:
        return f"{label}: ringing now"

    if entry.kind is AlarmKind.TIMER:
        return f"{label}: {format_duration(entry.remaining_s)} remaining"

    when = datetime.fromtimestamp(entry.time_epoch_ms / 1000)
    clock = _clock_words(when.hour, when.minute)
    if entry.days:
        if len(entry.days) == 7:
            day_words = "every day"
        elif entry.days == list(DAY_NAMES[:5]):
            day_words = "every weekday"
        else:
            day_words = "every " + ", ".join(_FULL_DAY[d] for d in entry.days)
        return f"{label}: {clock} {day_words}"

    if when.date() == now.date():
        return f"{label}: {clock} today"
    if when.date() == (now + timedelta(days=1)).date():
        return f"{label}: {clock} tomorrow"
    return f"{label}: {clock} on {when:%A}"


def format_duration(seconds: float) -> str:
    """``3725`` -> ``"1 hour 2 minutes"``. Rounded, because it is spoken."""
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total} second{'s' if total != 1 else ''}"
    parts = []
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs and not hours:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts)


def summarise_state(state: AlarmStateMsg, now: datetime | None = None) -> dict[str, Any]:
    """The tool response shape: machine fields plus a spoken-language summary.

    Both halves matter. The structured lists let a follow-up call cancel by id;
    the sentence is what stops the model inventing a time it was never told.
    """
    now = now or datetime.now()
    alarms = [describe_entry(a, now) for a in state.alarms]
    timers = [describe_entry(t, now) for t in state.timers]

    if alarms and timers:
        spoken = "; ".join(alarms + timers)
    elif alarms:
        spoken = "; ".join(alarms)
    elif timers:
        spoken = "; ".join(timers)
    else:
        spoken = "Nothing is set."

    out: dict[str, Any] = {
        "ok": not state.error,
        "alarms": [a.to_dict() for a in state.alarms],
        "timers": [t.to_dict() for t in state.timers],
        "summary": spoken,
    }
    if state.error:
        out["error"] = state.error
    if not state.exact_allowed:
        # Android 12+ can refuse exact alarms. The device still rings, but the
        # model must not promise a minute it cannot guarantee.
        out["warning"] = (
            "The display cannot schedule exact alarms, so this may fire a few "
            "minutes late. Tell the user to allow exact alarms in Android settings."
        )
    return out


def summarise_stopwatch(state: StopwatchStateMsg) -> dict[str, Any]:
    """Tool response shape for the stopwatch (v1.6).

    The elapsed is a plain ``elapsed_s`` the model can re-read aloud plus the
    spoken summary; laps arrive as their delta from the previous lap, which is
    what a person means when they ask \"what were my laps?\".
    """
    laps: list[dict[str, Any]] = []
    prev = 0
    for lap_ms in state.laps_ms:
        laps.append({"lap_s": round((lap_ms - prev) / 1000, 1)})
        prev = lap_ms
    spoken = (
        "The stopwatch is running at "
        + format_duration(state.elapsed_ms / 1000)
        + ("." if not laps else f" with {len(laps)} lap(s).")
    ) if state.running else (
        "The stopwatch is stopped at "
        + format_duration(state.elapsed_ms / 1000)
        + ("." if not laps else f" with {len(laps)} lap(s).")
    )
    return {
        "ok": True,
        "running": state.running,
        "elapsed_s": round(state.elapsed_ms / 1000, 1),
        "laps": laps,
        "summary": spoken,
    }


# ---------------------------------------------------------------------------
# Command / ack round trip
# ---------------------------------------------------------------------------


class AlarmCoordinator:
    """Sends ``alarm_command`` and matches the ``alarm_state`` that answers it.

    ``send`` returns False when no device is attached, which is the difference
    between "the alarm is set" and "I could not reach the display" -- a
    distinction the user very much cares about at bedtime.
    """

    def __init__(
        self,
        send: Callable[[AlarmCommand], bool],
        *,
        timeout_s: float = DEFAULT_ACK_TIMEOUT_S,
    ) -> None:
        self._send = send
        self._timeout_s = timeout_s
        self._pending: dict[str, asyncio.Future[AlarmStateMsg]] = {}
        self.last_state: AlarmStateMsg | None = None
        # Stopwatch acks (v1.6, CLOCK-BRIEF-STOPWATCH) answer as
        # StopwatchStateMsg rather than AlarmStateMsg, so they wait on their own
        # map; the device pushes one on every transition.
        self._sw_pending: dict[str, asyncio.Future[StopwatchStateMsg]] = {}
        self.last_stopwatch: StopwatchStateMsg | None = None

    def on_state(self, state: AlarmStateMsg) -> None:
        """Called by the hub for every ``alarm_state`` the device sends.

        Unsolicited snapshots (a timer counting down, a user tapping Dismiss on
        the panel) carry no ``req_id`` and simply refresh the cache.
        """
        self.last_state = state
        future = self._pending.pop(state.req_id, None) if state.req_id else None
        if future is not None and not future.done():
            future.set_result(state)

    def on_stopwatch_state(self, state: StopwatchStateMsg) -> None:
        """Called by the hub for every ``stopwatch_state`` push (v1.6).

        Unsolicited pushes (the user touched the panel) carry no ``req_id`` and
        simply refresh the cached stopwatch.
        """
        self.last_stopwatch = state
        future = self._sw_pending.pop(state.req_id, None) if state.req_id else None
        if future is not None and not future.done():
            future.set_result(state)

    def disconnected(self) -> None:
        """Do not confirm requests or retain cached state across device replacement."""
        self.last_state = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DeviceOffline("The display disconnected; the change was not confirmed."))
        self._pending.clear()
        self.last_stopwatch = None
        for future in self._sw_pending.values():
            if not future.done():
                future.set_exception(DeviceOffline("The display disconnected; the stopwatch state is unknown."))
        self._sw_pending.clear()

    async def request_stopwatch(self, op: AlarmOp) -> StopwatchStateMsg:
        """Send a stopwatch op and wait for its ``stopwatch_state`` ack (v1.6)."""
        req_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[StopwatchStateMsg] = loop.create_future()
        self._sw_pending[req_id] = future
        command = AlarmCommand(op=op, req_id=req_id)
        try:
            if not self._send(command):
                raise DeviceOffline(
                    "The display is not connected, so the stopwatch could not be reached."
                )
            return await asyncio.wait_for(future, timeout=self._timeout_s)
        except asyncio.TimeoutError as exc:
            raise AckTimeout(
                "The display did not confirm the stopwatch change within "
                f"{self._timeout_s:g} seconds."
            ) from exc
        finally:
            self._sw_pending.pop(req_id, None)

    # --- stopwatch conveniences (v1.6, CLOCK-BRIEF-STOPWATCH) ----------------

    async def stopwatch_start(self) -> StopwatchStateMsg:
        return await self.request_stopwatch(AlarmOp.STOPWATCH_START)

    async def stopwatch_pause(self) -> StopwatchStateMsg:
        return await self.request_stopwatch(AlarmOp.STOPWATCH_PAUSE)

    async def stopwatch_reset(self) -> StopwatchStateMsg:
        return await self.request_stopwatch(AlarmOp.STOPWATCH_RESET)

    async def stopwatch_lap(self) -> StopwatchStateMsg:
        return await self.request_stopwatch(AlarmOp.STOPWATCH_LAP)

    async def stopwatch_status(self) -> StopwatchStateMsg:
        """Fresh status via a round trip (the cached push may be stale)."""
        return await self.request_stopwatch(AlarmOp.STOPWATCH_STATUS)

    async def request(self, command: AlarmCommand) -> AlarmStateMsg:
        """Send ``command`` and wait for its acknowledgement.

        Raises ``DeviceOffline`` if there is nothing to send to and ``AckTimeout``
        if the device took the command but never answered.
        """
        req_id = command.req_id or uuid.uuid4().hex[:12]
        command.req_id = req_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[AlarmStateMsg] = loop.create_future()
        self._pending[req_id] = future

        try:
            if not self._send(command):
                raise DeviceOffline(
                    "The display is not connected, so nothing could be scheduled."
                )
            return await asyncio.wait_for(future, timeout=self._timeout_s)
        except asyncio.TimeoutError as exc:
            raise AckTimeout(
                "The display did not confirm within "
                f"{self._timeout_s:g} seconds; it may or may not have been set."
            ) from exc
        finally:
            self._pending.pop(req_id, None)

    # --- convenience builders, used by the Gemini tool handlers -------------

    async def set_alarm(
        self, label: str, time_epoch_ms: int, days: list[str] | None = None
    ) -> AlarmStateMsg:
        return await self.request(
            AlarmCommand(
                op=AlarmOp.SET_ALARM,
                label=label,
                time_epoch_ms=time_epoch_ms,
                days=days or [],
            )
        )

    async def set_timer(self, label: str, duration_s: float) -> AlarmStateMsg:
        return await self.request(
            AlarmCommand(op=AlarmOp.SET_TIMER, label=label, duration_s=duration_s)
        )

    async def cancel(
        self, *, entry_id: str = "", label: str = "", kind: str = ""
    ) -> AlarmStateMsg:
        return await self.request(
            AlarmCommand(op=AlarmOp.CANCEL, id=entry_id, label=label, kind=kind)
        )

    async def list_all(self) -> AlarmStateMsg:
        return await self.request(AlarmCommand(op=AlarmOp.LIST))
