"""The ambient weather path: parsing Open-Meteo, the poller, and the envelope.

Nothing here touches the network. ``WeatherPoller`` takes an injectable fetch
for exactly this reason, and ``conftest.py`` sets ``WEATHER_ENABLED=false`` so
no other test can accidentally reach out either.

The behaviour that matters most is the failure behaviour: a home internet
connection drops, and when it does the server must keep serving voice with a
stale temperature on the card rather than falling over.
"""

from __future__ import annotations

import asyncio

import pytest

import protocol as P
from weather import Reading, WeatherError, WeatherPoller, parse_current


def open_meteo(temp=18.4, code=3, is_day=1) -> dict:
    """A response shaped the way the real endpoint shapes one."""
    return {
        "latitude": 43.0481,
        "longitude": -76.1474,
        "timezone": "America/New_York",
        "current_units": {"temperature_2m": "°C", "weather_code": "wmo code"},
        "current": {
            "time": "2026-09-06T14:00",
            "interval": 900,
            "temperature_2m": temp,
            "weather_code": code,
            "is_day": is_day,
        },
    }


# --- parsing ----------------------------------------------------------------


def test_parses_a_real_shaped_response() -> None:
    reading = parse_current(open_meteo(temp=18.44, code=61, is_day=1))
    assert reading.temp_c == 18.4
    assert reading.code == 61
    assert reading.is_day is True
    assert reading.fetched_at > 0


def test_is_day_arrives_as_an_integer_not_a_boolean() -> None:
    """Open-Meteo sends 1/0, and a truthiness bug here means a sun glyph at
    midnight."""
    assert parse_current(open_meteo(is_day=0)).is_day is False
    assert parse_current(open_meteo(is_day=1)).is_day is True


def test_sub_zero_temperatures_survive() -> None:
    assert parse_current(open_meteo(temp=-12.5, code=75)).temp_c == -12.5


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"current": None},
        {"current": {}},
        {"current": {"temperature_2m": "warm", "weather_code": 0}},
        {"current": {"temperature_2m": True, "weather_code": 0}},
        {"current": {"temperature_2m": float("nan"), "weather_code": 0}},
        {"current": {"temperature_2m": 10, "weather_code": "cloudy"}},
        {"current": {"temperature_2m": 10, "weather_code": 0, "is_day": "yes"}},
    ],
)
def test_unreadable_responses_raise_rather_than_guess(payload) -> None:
    with pytest.raises(WeatherError):
        parse_current(payload)


# --- the poller -------------------------------------------------------------


async def test_a_successful_poll_reports_once_and_caches() -> None:
    seen: list[Reading] = []
    poller = WeatherPoller(
        43.0, -76.0, on_reading=seen.append, fetch=lambda: _immediately(open_meteo())
    )
    reading = await poller.poll_once()
    assert reading is not None
    assert poller.last is reading
    assert [r.temp_c for r in seen] == [18.4]


async def test_a_failed_fetch_keeps_the_last_reading_and_stays_quiet() -> None:
    """The whole point: no exception escapes, no callback fires, and the card
    keeps showing the last thing we knew."""
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            return _immediately(open_meteo(temp=21.0))
        raise OSError("the router is rebooting")

    seen: list[Reading] = []
    poller = WeatherPoller(43.0, -76.0, on_reading=seen.append, fetch=fetch)

    await poller.poll_once()
    assert await poller.poll_once() is None

    assert poller.last is not None and poller.last.temp_c == 21.0
    assert len(seen) == 1, "a failed poll must not push anything at the device"
    assert poller.failures == 1


async def test_a_garbage_response_is_a_failure_not_a_crash() -> None:
    poller = WeatherPoller(43.0, -76.0, fetch=lambda: _immediately({"nope": True}))
    assert await poller.poll_once() is None
    assert poller.last is None


async def test_a_throwing_callback_cannot_break_the_poller() -> None:
    def explode(_: Reading) -> None:
        raise RuntimeError("the UI push failed")

    poller = WeatherPoller(
        43.0, -76.0, on_reading=explode, fetch=lambda: _immediately(open_meteo())
    )
    assert await poller.poll_once() is not None
    assert poller.last is not None


async def test_run_polls_immediately_and_can_be_cancelled() -> None:
    """A server that has just started must have something to send the moment a
    device says hello, rather than waiting out the first interval."""
    seen: list[Reading] = []
    poller = WeatherPoller(
        43.0,
        -76.0,
        interval_s=3600,
        on_reading=seen.append,
        fetch=lambda: _immediately(open_meteo()),
    )
    poller.start()
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.01)
    await poller.stop()
    assert len(seen) == 1


async def test_refresh_soon_cuts_the_wait_short() -> None:
    seen: list[Reading] = []
    poller = WeatherPoller(
        43.0,
        -76.0,
        interval_s=3600,
        on_reading=seen.append,
        fetch=lambda: _immediately(open_meteo()),
    )
    poller.start()
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.01)
    poller.refresh_soon()
    for _ in range(50):
        if len(seen) > 1:
            break
        await asyncio.sleep(0.01)
    await poller.stop()
    assert len(seen) >= 2


def test_the_poll_interval_has_a_floor() -> None:
    assert WeatherPoller(43.0, -76.0, interval_s=1).interval_s == 60.0


# --- the wire envelope ------------------------------------------------------


def test_weather_message_round_trips() -> None:
    msg = P.WeatherMsg(temp_c=-3.5, code=71, is_day=False)
    decoded = P.decode(msg.encode())
    assert isinstance(decoded, P.WeatherMsg)
    assert decoded.fields() == {"temp_c": -3.5, "code": 71, "is_day": False}


def test_weather_is_announced_in_the_protocol_minor() -> None:
    """The client hand-mirrors this file; the minor is how each end logs skew."""
    assert P.PROTOCOL_MINOR >= 2
    assert P.MsgType.WEATHER.value == "weather"


@pytest.mark.parametrize(
    "body",
    [
        '{"v":1,"t":"weather","code":0,"is_day":true}',
        '{"v":1,"t":"weather","temp_c":"mild","code":0}',
        '{"v":1,"t":"weather","temp_c":true,"code":0}',
        '{"v":1,"t":"weather","temp_c":10,"code":1.5}',
        '{"v":1,"t":"weather","temp_c":10,"code":true}',
    ],
)
def test_malformed_weather_is_a_protocol_error(body: str) -> None:
    with pytest.raises(P.ProtocolError):
        P.decode(body)


def _immediately(value):
    """A coroutine that is already done -- lets the tests pass plain lambdas."""

    async def done():
        return value

    return done()
