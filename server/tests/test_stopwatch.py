"""Stopwatch (CLOCK-BRIEF-STOPWATCH, v1.6): protocol, coordinator round trip,
and the voice-tool wiring."""

from __future__ import annotations

import asyncio
import pytest

import protocol as P
from alarms import AlarmCoordinator, summarise_stopwatch


def test_stopwatch_state_round_trips() -> None:
    raw = '{"v":1,"t":"stopwatch_state","running":true,"elapsed_ms":125000,"laps_ms":[10000,25000]}'
    msg = P.decode(raw)
    assert isinstance(msg, P.StopwatchStateMsg)
    assert msg.running is True
    assert msg.elapsed_ms == 125000
    assert msg.laps_ms == [10000, 25000]
    encoded = msg.encode()
    out = P.decode(encoded)
    assert out.elapsed_ms == 125000 and len(out.laps_ms) == 2


def test_stopwatch_state_rejects_garbage() -> None:
    with pytest.raises(P.ProtocolError):
        P.decode('{"v":1,"t":"stopwatch_state","elapsed_ms":-5}')
    with pytest.raises(P.ProtocolError):
        P.decode('{"v":1,"t":"stopwatch_state","laps_ms":["x"]}')


async def test_coordinator_round_trip_confirms_from_device() -> None:
    sent: list[P.AlarmCommand] = []

    def send(command: P.AlarmCommand) -> bool:
        sent.append(command)
        return True

    coordinator = AlarmCoordinator(send)

    async def ack_later() -> None:
        await asyncio.sleep(0.01)
        command = sent[-1]
        coordinator.on_stopwatch_state(
            P.StopwatchStateMsg(running=True, elapsed_ms=3_000, req_id=command.req_id)
        )

    task = asyncio.create_task(ack_later())
    state = await coordinator.stopwatch_start()
    await task
    assert sent[-1].op is P.AlarmOp.STOPWATCH_START
    assert state.running is True and state.elapsed_ms == 3_000
    assert coordinator.last_stopwatch is state


async def test_summary_sentence() -> None:
    summary = summarise_stopwatch(
        P.StopwatchStateMsg(running=False, elapsed_ms=65_000, laps_ms=[10_000, 25_000])
    )
    assert "stopped at" in summary["summary"]
    assert summary["elapsed_s"] == 65.0
    assert len(summary["laps"]) == 2
    assert summary["laps"][1]["lap_s"] == 15.0  # delta from the previous lap
