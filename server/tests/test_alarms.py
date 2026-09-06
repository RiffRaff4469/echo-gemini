"""Alarms and timers: time resolution, the command/ack round trip, and the
Gemini tool handlers.

The device owns the schedule, so what these tests pin down is everything the
server can still get wrong: turning "seven" into the right epoch, refusing to
claim success when the display never answered, and answering a tool call with
the state the device confirmed rather than the state that was requested.

The fake device's ``FakeSchedule`` stands in for ``AlarmScheduler.kt`` so a tool
call can be driven end to end without hardware.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import protocol as P  # noqa: E402
from alarms import (  # noqa: E402
    AckTimeout,
    AlarmCoordinator,
    AlarmError,
    DeviceOffline,
    describe_entry,
    format_duration,
    normalise_days,
    parse_clock,
    resolve_alarm_time,
    resolve_duration,
    summarise_state,
)
from fake_device import FakeSchedule  # noqa: E402
from gemini_live import (  # noqa: E402
    CANCEL_ALARM_TOOL,
    CANCEL_TIMER_TOOL,
    LIST_ALARMS_TOOL,
    SET_ALARM_TOOL,
    SET_TIMER_TOOL,
    LiveSessionManager,
)

# pytest puts this directory on sys.path (there is no tests/__init__.py), so the
# session fakes are reused rather than re-written.
from test_session import (  # noqa: E402
    FakeConnector,
    FakeLiveSession,
    RecordingSink,
    make_config,
    settle,
)

# A fixed reference point, so "tomorrow" in a test never depends on when the
# suite runs: Wednesday 9 September 2026, 09:30 local.
WEDNESDAY_0930 = datetime(2026, 9, 9, 9, 30)


# --- clock parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("7", (7, 0)),
        ("7:05", (7, 5)),
        ("07:05", (7, 5)),
        ("19:30", (19, 30)),
        ("7:05 am", (7, 5)),
        ("7:05 PM", (19, 5)),
        ("7pm", (19, 0)),
        ("12 am", (0, 0)),   # midnight, not noon
        ("12 pm", (12, 0)),  # noon, not midnight
        ("12:01 a.m.", (0, 1)),
        ("  9:15  ", (9, 15)),
    ],
)
def test_parse_clock_accepts_what_a_model_actually_says(text, expected) -> None:
    assert parse_clock(text) == expected


@pytest.mark.parametrize("text", ["", "half past", "25:00", "7:75", "13 pm", "0 am"])
def test_parse_clock_rejects_nonsense(text) -> None:
    with pytest.raises(AlarmError):
        parse_clock(text)


# --- resolving to an epoch --------------------------------------------------


def at(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000)


def test_a_time_later_today_stays_today() -> None:
    when = at(resolve_alarm_time("17:00", now=WEDNESDAY_0930))
    assert (when.date(), when.hour) == (WEDNESDAY_0930.date(), 17)


def test_a_time_already_past_rolls_to_tomorrow() -> None:
    # "Set an alarm for seven" at half past nine in the morning means tomorrow.
    when = at(resolve_alarm_time("7:00", now=WEDNESDAY_0930))
    assert when.day == WEDNESDAY_0930.day + 1
    assert (when.hour, when.minute) == (7, 0)


def test_seconds_are_zeroed_so_an_alarm_fires_on_the_minute() -> None:
    when = at(resolve_alarm_time("17:00", now=WEDNESDAY_0930.replace(second=47)))
    assert (when.second, when.microsecond) == (0, 0)


def test_repeating_days_pick_the_next_matching_weekday() -> None:
    # Wednesday morning, asking for 7am on Mondays and Fridays -> Friday.
    when = at(resolve_alarm_time("7:00", days=["mon", "fri"], now=WEDNESDAY_0930))
    assert when.weekday() == 4  # Friday
    assert when.hour == 7


def test_a_repeating_day_can_still_be_today_if_the_time_is_ahead() -> None:
    when = at(resolve_alarm_time("17:00", days=["wed"], now=WEDNESDAY_0930))
    assert when.date() == WEDNESDAY_0930.date()


def test_an_explicit_date_is_honoured() -> None:
    when = at(resolve_alarm_time("6:45", date="2026-12-25", now=WEDNESDAY_0930))
    assert (when.month, when.day, when.hour, when.minute) == (12, 25, 6, 45)


def test_an_iso_datetime_from_the_model_is_accepted() -> None:
    when = at(resolve_alarm_time("2026-09-10T07:05", now=WEDNESDAY_0930))
    assert (when.day, when.hour, when.minute) == (10, 7, 5)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"when": "2020-01-01T07:00"},          # ISO in the past
        {"when": "7:00", "date": "2020-01-01"},  # explicit date in the past
        {"when": "7:00", "date": "not-a-date"},
        {"when": "2026-13-45T07:00"},
    ],
)
def test_times_in_the_past_or_garbage_are_refused(kwargs) -> None:
    # An alarm silently set for last January is worse than being told no.
    with pytest.raises(AlarmError):
        resolve_alarm_time(kwargs.pop("when"), now=WEDNESDAY_0930, **kwargs)


# --- durations and day names ------------------------------------------------


def test_duration_units_are_combined() -> None:
    assert resolve_duration(30) == 30
    assert resolve_duration(None, minutes=10) == 600
    assert resolve_duration(None, hours=1, minutes=30) == 5400
    assert resolve_duration(5, minutes=1) == 65


@pytest.mark.parametrize("kwargs", [{"duration_s": 0}, {"duration_s": -5}, {}])
def test_a_timer_needs_a_positive_duration(kwargs) -> None:
    with pytest.raises(AlarmError):
        resolve_duration(**kwargs)


def test_timers_are_capped_at_a_day() -> None:
    with pytest.raises(AlarmError):
        resolve_duration(P.MAX_TIMER_DURATION_S + 1)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (["monday", "Tue"], ["mon", "tue"]),
        ("weekdays", ["mon", "tue", "wed", "thu", "fri"]),
        ("weekends", ["sat", "sun"]),
        ("daily", list(P.DAY_NAMES)),
        (["sun", "mon"], ["mon", "sun"]),  # normalised to Monday-first
        (None, []),
    ],
)
def test_day_names_accept_what_a_model_says(raw, expected) -> None:
    assert normalise_days(raw) == expected


def test_an_invented_day_is_refused() -> None:
    with pytest.raises(AlarmError):
        normalise_days(["funday"])


# --- reading state back out loud --------------------------------------------


def test_format_duration_is_spoken_not_printed() -> None:
    assert format_duration(45) == "45 seconds"
    assert format_duration(60) == "1 minute"
    assert format_duration(605) == "10 minutes 5 seconds"
    assert format_duration(3725) == "1 hour 2 minutes"


def test_describe_entry_says_today_tomorrow_or_the_weekday() -> None:
    def alarm(hour, day=9, days=None):
        when = WEDNESDAY_0930.replace(day=day, hour=hour, minute=5)
        return P.AlarmEntry(
            id="a",
            kind=P.AlarmKind.ALARM,
            label="Wake up",
            time_epoch_ms=int(when.timestamp() * 1000),
            days=days or [],
        )

    assert "today" in describe_entry(alarm(17), WEDNESDAY_0930)
    assert "tomorrow" in describe_entry(alarm(7, day=10), WEDNESDAY_0930)
    assert "Friday" in describe_entry(alarm(7, day=11), WEDNESDAY_0930)
    assert "5:05 pm" in describe_entry(alarm(17), WEDNESDAY_0930)
    assert "every weekday" in describe_entry(
        alarm(7, day=10, days=["mon", "tue", "wed", "thu", "fri"]), WEDNESDAY_0930
    )
    assert "every day" in describe_entry(
        alarm(7, day=10, days=list(P.DAY_NAMES)), WEDNESDAY_0930
    )


def test_summary_of_an_empty_schedule_is_a_sentence_not_an_empty_list() -> None:
    summary = summarise_state(P.AlarmStateMsg())
    assert summary["summary"] == "Nothing is set."
    assert summary["alarms"] == [] and summary["timers"] == []


def test_summary_warns_when_the_device_cannot_schedule_exact_alarms() -> None:
    # Android 12+ can refuse SCHEDULE_EXACT_ALARM. The model must not promise a
    # minute the device cannot keep.
    summary = summarise_state(P.AlarmStateMsg(exact_allowed=False))
    assert "exact" in summary["warning"].lower()


def test_summary_carries_the_ids_a_follow_up_cancel_needs() -> None:
    state = P.AlarmStateMsg(
        timers=[
            P.AlarmEntry(id="t9", kind=P.AlarmKind.TIMER, label="Pasta", remaining_s=90)
        ]
    )
    summary = summarise_state(state)
    assert summary["timers"][0]["id"] == "t9"
    assert "1 minute 30 seconds" in summary["summary"]


# --- the command / ack round trip -------------------------------------------


class Wire:
    """A device on the other end of the coordinator, backed by FakeSchedule."""

    def __init__(self, *, answer: bool = True, schedule: FakeSchedule | None = None):
        self.schedule = schedule or FakeSchedule()
        self.answer = answer
        self.sent: list[P.AlarmCommand] = []
        self.online = True
        self.coordinator = AlarmCoordinator(self.send, timeout_s=0.2)

    def send(self, cmd: P.AlarmCommand) -> bool:
        self.sent.append(cmd)
        if not self.online:
            return False
        if self.answer:
            error = self.schedule.apply(cmd)
            alarms, timers = self.schedule.snapshot()
            self.coordinator.on_state(
                P.AlarmStateMsg(
                    alarms=alarms, timers=timers, req_id=cmd.req_id, error=error
                )
            )
        return True


async def test_a_command_is_acknowledged_with_the_resulting_state() -> None:
    wire = Wire()
    state = await wire.coordinator.set_timer("Pasta", 600)
    assert [t.label for t in state.timers] == ["Pasta"]
    assert wire.sent[0].op is P.AlarmOp.SET_TIMER
    assert wire.sent[0].req_id, "every command must carry a correlation id"


async def test_an_unsolicited_state_does_not_satisfy_a_pending_command() -> None:
    """A timer ticking down pushes state constantly. Treating the next one to
    arrive as the ack would let the model confirm a command the device never
    applied."""
    wire = Wire(answer=False)
    task = asyncio.create_task(wire.coordinator.set_timer("Pasta", 600))
    await settle()

    wire.coordinator.on_state(P.AlarmStateMsg(req_id=""))  # unrelated push
    await settle()
    assert not task.done()

    wire.coordinator.on_state(P.AlarmStateMsg(req_id=wire.sent[0].req_id, timers=[]))
    assert (await task).req_id == wire.sent[0].req_id


async def test_a_stale_ack_for_another_request_is_ignored() -> None:
    wire = Wire(answer=False)
    task = asyncio.create_task(wire.coordinator.list_all())
    await settle()
    wire.coordinator.on_state(P.AlarmStateMsg(req_id="some-other-request"))
    with pytest.raises(AckTimeout):
        await task


async def test_no_device_is_reported_as_offline_not_as_success() -> None:
    wire = Wire()
    wire.online = False
    with pytest.raises(DeviceOffline):
        await wire.coordinator.set_alarm("Wake up", 1_800_000_000_000)


async def test_a_silent_device_times_out_rather_than_hanging_the_conversation() -> None:
    wire = Wire(answer=False)
    with pytest.raises(AckTimeout):
        await wire.coordinator.list_all()


async def test_unsolicited_state_refreshes_the_cache_for_health() -> None:
    wire = Wire()
    assert wire.coordinator.last_state is None
    wire.coordinator.on_state(
        P.AlarmStateMsg(alarms=[P.AlarmEntry(id="a1", kind=P.AlarmKind.ALARM)])
    )
    assert len(wire.coordinator.last_state.alarms) == 1


# --- the device's own scheduler semantics (mirrored by AlarmScheduler.kt) ----


def test_cancel_by_label_removes_the_right_entry() -> None:
    schedule = FakeSchedule()
    schedule.apply(P.AlarmCommand(op=P.AlarmOp.SET_TIMER, label="Pasta", duration_s=60))
    schedule.apply(P.AlarmCommand(op=P.AlarmOp.SET_TIMER, label="Eggs", duration_s=60))
    assert schedule.apply(P.AlarmCommand(op=P.AlarmOp.CANCEL, label="eggs")) == ""
    assert [t.label for t in schedule.snapshot()[1]] == ["Pasta"]


def test_cancel_with_no_identifier_is_refused_when_ambiguous() -> None:
    schedule = FakeSchedule()
    schedule.apply(P.AlarmCommand(op=P.AlarmOp.SET_TIMER, label="A", duration_s=60))
    schedule.apply(P.AlarmCommand(op=P.AlarmOp.SET_TIMER, label="B", duration_s=60))
    error = schedule.apply(P.AlarmCommand(op=P.AlarmOp.CANCEL))
    assert "which one" in error
    assert len(schedule.snapshot()[1]) == 2, "an ambiguous cancel must delete nothing"


def test_cancel_kind_does_not_cross_over() -> None:
    schedule = FakeSchedule()
    schedule.apply(
        P.AlarmCommand(op=P.AlarmOp.SET_ALARM, label="Same", time_epoch_ms=1)
    )
    schedule.apply(P.AlarmCommand(op=P.AlarmOp.SET_TIMER, label="Same", duration_s=60))
    schedule.apply(
        P.AlarmCommand(op=P.AlarmOp.CANCEL, label="Same", kind=P.AlarmKind.TIMER.value)
    )
    alarms, timers = schedule.snapshot()
    assert len(alarms) == 1 and timers == []


def test_a_finished_timer_fires_once_and_disappears() -> None:
    now = [1000.0]
    schedule = FakeSchedule(clock=lambda: now[0])
    schedule.apply(P.AlarmCommand(op=P.AlarmOp.SET_TIMER, label="Pasta", duration_s=5))
    assert schedule.due() == []
    now[0] += 6
    fired = schedule.due()
    assert [f.label for f in fired] == ["Pasta"]
    assert schedule.due() == [], "a timer must not ring twice"
    assert schedule.snapshot()[1] == []


def test_a_repeating_alarm_rolls_forward_instead_of_disappearing() -> None:
    fire_at = WEDNESDAY_0930.replace(hour=7, minute=0)
    now = [fire_at.timestamp() + 1]
    schedule = FakeSchedule(clock=lambda: now[0])
    schedule.apply(
        P.AlarmCommand(
            op=P.AlarmOp.SET_ALARM,
            label="Work",
            time_epoch_ms=int(fire_at.timestamp() * 1000),
            days=["mon", "wed", "fri"],
        )
    )
    assert [f.label for f in schedule.due()] == ["Work"]
    remaining = schedule.snapshot()[0]
    assert len(remaining) == 1, "a repeating alarm survives firing"
    assert at(remaining[0].time_epoch_ms).weekday() == 4  # next is Friday


# --- Gemini tool handlers ---------------------------------------------------


class AlarmSink(RecordingSink):
    """A sink that also carries an alarm coordinator, as ``Hub`` does."""

    def __init__(self) -> None:
        super().__init__()
        self.wire = Wire()
        self.alarms = self.wire.coordinator


@pytest.fixture
async def tools():
    session = FakeLiveSession()
    sink = AlarmSink()
    manager = LiveSessionManager(make_config(), sink, connector=FakeConnector(session))
    await manager.start("test")
    await settle()
    try:
        yield manager, session, sink
    finally:
        await manager.stop("teardown")


async def call(manager: LiveSessionManager, name: str, **args) -> dict:
    """Invoke a tool the way ``_pump_down`` does, and return its response."""
    call_obj = type("Call", (), {"name": name, "id": "c1", "args": args})()
    return await manager._run_tool(call_obj)


async def test_set_alarm_tool_confirms_what_the_device_accepted(tools) -> None:
    manager, _session, sink = tools
    result = await call(manager, SET_ALARM_TOOL, time="7:05 am", label="Wake up")
    assert result["ok"] is True
    assert result["alarms"][0]["label"] == "Wake up"
    assert "7:05 am" in result["summary"]
    # And the device really was asked, with an absolute time.
    sent = sink.wire.sent[-1]
    assert sent.op is P.AlarmOp.SET_ALARM
    assert at(sent.time_epoch_ms).hour == 7


async def test_set_alarm_tool_passes_repeat_days_through(tools) -> None:
    manager, _session, sink = tools
    result = await call(manager, SET_ALARM_TOOL, time="7:00", days="weekdays")
    assert sink.wire.sent[-1].days == ["mon", "tue", "wed", "thu", "fri"]
    assert "every weekday" in result["summary"]


async def test_set_timer_tool_accepts_minutes_the_model_volunteers(tools) -> None:
    manager, _session, sink = tools
    result = await call(manager, SET_TIMER_TOOL, minutes=10, label="Pasta")
    assert sink.wire.sent[-1].duration_s == 600
    assert "Pasta" in result["summary"]


async def test_a_bad_time_comes_back_as_an_error_the_model_can_say(tools) -> None:
    manager, _session, sink = tools
    result = await call(manager, SET_ALARM_TOOL, time="half past tea")
    assert "error" in result
    assert not sink.wire.sent, "nothing should reach the device"


async def test_cancel_timer_tool_cancels_only_the_timer(tools) -> None:
    manager, _session, sink = tools
    await call(manager, SET_ALARM_TOOL, time="7:00", label="Same")
    await call(manager, SET_TIMER_TOOL, duration_s=300, label="Same")
    result = await call(manager, CANCEL_TIMER_TOOL, label="Same")
    assert result["timers"] == []
    assert len(result["alarms"]) == 1


async def test_cancel_alarm_tool_reports_a_miss_instead_of_claiming_success(
    tools,
) -> None:
    manager, _session, _sink = tools
    result = await call(manager, CANCEL_ALARM_TOOL, label="Nonexistent")
    assert result["ok"] is False
    assert "Nonexistent" in result["error"]


async def test_list_alarms_tool_reads_back_everything(tools) -> None:
    manager, _session, _sink = tools
    await call(manager, SET_ALARM_TOOL, time="7:00", label="Wake up")
    await call(manager, SET_TIMER_TOOL, duration_s=300, label="Pasta")
    result = await call(manager, LIST_ALARMS_TOOL)
    assert len(result["alarms"]) == 1 and len(result["timers"]) == 1
    assert "Wake up" in result["summary"] and "Pasta" in result["summary"]


async def test_an_offline_device_is_admitted_to_not_promised(tools) -> None:
    manager, _session, sink = tools
    sink.wire.online = False
    result = await call(manager, SET_TIMER_TOOL, duration_s=60)
    assert "not connected" in result["error"]


async def test_a_silent_device_does_not_hang_the_session(tools) -> None:
    manager, _session, sink = tools
    sink.wire.answer = False
    result = await call(manager, LIST_ALARMS_TOOL)
    assert "did not confirm" in result["error"]
    assert manager.active, "a timed-out tool must not take the session down"


async def test_tool_calls_keep_the_billed_session_alive(tools) -> None:
    manager, _session, _sink = tools
    await asyncio.sleep(0.05)
    await call(manager, LIST_ALARMS_TOOL)
    assert manager.idle_seconds < 0.05


async def test_a_sink_without_alarms_answers_instead_of_crashing() -> None:
    """The camera-only sink (any older Hub) must degrade to a sentence."""
    session = FakeLiveSession()
    manager = LiveSessionManager(
        make_config(), RecordingSink(), connector=FakeConnector(session)
    )
    await manager.start("test")
    await settle()
    result = await call(manager, LIST_ALARMS_TOOL)
    assert "error" in result
    await manager.stop("done")


async def test_alarm_tools_are_declared_even_with_vision_off() -> None:
    """Alarms need no camera, so VISION_MODE must not silently remove them."""
    pytest.importorskip("google.genai")
    from gemini_live import _build_tools

    names = {
        d.name
        for tool in _build_tools(make_config(vision_mode="off"))
        for d in tool.function_declarations
    }
    assert SET_ALARM_TOOL in names and LIST_ALARMS_TOOL in names
    assert "start_camera" not in names


# --- alarm_fired relay ------------------------------------------------------


async def test_a_fired_alarm_is_narrated_to_an_open_session(tools) -> None:
    manager, session, _sink = tools
    await manager.note_alarm_fired(P.AlarmKind.TIMER, "Pasta")
    assert session.text_in, "an open session must be told the timer went off"
    assert "Pasta" in session.text_in[0]


async def test_a_fired_alarm_without_a_session_is_a_noop() -> None:
    # The device is already chiming; the server having no session is not a
    # failure, and must not raise.
    manager = LiveSessionManager(make_config(), RecordingSink())
    await manager.note_alarm_fired(P.AlarmKind.ALARM, "Wake up")


def test_tomorrow_is_preserved_even_when_the_clock_time_is_still_ahead():
    when = at(resolve_alarm_time("tomorrow at 7pm", now=WEDNESDAY_0930))
    assert (when.day, when.hour) == (10, 19)


def test_today_in_the_past_is_not_silently_changed_to_tomorrow():
    with pytest.raises(AlarmError):
        resolve_alarm_time("today 7am", now=WEDNESDAY_0930)


def test_iso_offset_is_honoured():
    from datetime import timezone
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    actual = resolve_alarm_time("2026-09-10T07:00:00-04:00", now=now)
    assert actual == int(datetime(2026, 9, 10, 11, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 0.00001])
def test_nonfinite_and_submillisecond_timers_are_rejected(value):
    with pytest.raises(AlarmError):
        resolve_duration(value)
    with pytest.raises(P.ProtocolError):
        P.AlarmCommand(op=P.AlarmOp.SET_TIMER, duration_s=value)


async def test_disconnect_fails_pending_commands_and_clears_cached_state():
    wire = Wire(answer=False)
    wire.coordinator.on_state(P.AlarmStateMsg())
    task = asyncio.create_task(wire.coordinator.list_all())
    await settle()
    wire.coordinator.disconnected()
    with pytest.raises(DeviceOffline):
        await task
    assert wire.coordinator.last_state is None
    assert not wire.coordinator._pending


async def test_cancelled_tool_wait_does_not_leak_pending_ack():
    wire = Wire(answer=False)
    task = asyncio.create_task(wire.coordinator.list_all())
    await settle()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not wire.coordinator._pending


def test_ringing_state_is_not_reported_as_a_running_countdown():
    state = P.AlarmStateMsg(timers=[P.AlarmEntry(id="t", kind=P.AlarmKind.TIMER, ringing=True)])
    assert "ringing now" in summarise_state(state)["summary"]
