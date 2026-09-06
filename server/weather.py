"""Ambient weather for the device's home screen.

The Show has no direct internet by design (HANDOFF section 3): it dials one
outbound WebSocket to this server over the tailnet and gets everything through
it. So the weather it displays has to be fetched here and pushed down, the same
way display commands are.

Open-Meteo is used because it needs no API key and no account -- one anonymous
GET returns the current temperature, a WMO weather code and a day/night flag.
See https://open-meteo.com/en/docs.

**This must never be able to hurt the voice link.** The poller is an independent
asyncio task with its own short-lived HTTP session; every failure path keeps the
last good reading and logs, and nothing in ``main.py`` awaits it. A dead
internet connection costs the household a stale temperature and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

log = logging.getLogger("echo.weather")

API_URL = "https://api.open-meteo.com/v1/forecast"

# One request every ten minutes. Open-Meteo updates hourly, so this is already
# generous; the interval exists so a transient failure self-heals quickly, not
# because the data moves.
DEFAULT_POLL_S = 600.0

# Short on purpose. A weather fetch that hangs must not hold a task open for
# minutes; the next poll will pick it up.
REQUEST_TIMEOUT_S = 10.0


class WeatherError(RuntimeError):
    """A response we could not read. Always logged, never raised at a caller."""


@dataclass(frozen=True)
class Reading:
    """One observation, exactly the three fields the device renders."""

    temp_c: float
    code: int
    is_day: bool
    fetched_at: float = 0.0

    def age_s(self) -> float:
        return max(0.0, time.time() - self.fetched_at)


def parse_current(payload: Any) -> Reading:
    """Pull a [Reading] out of an Open-Meteo ``current`` block.

    Written defensively because this is the one place in the server that parses
    something from the public internet: anything unexpected raises
    ``WeatherError`` and the caller keeps the previous value.
    """
    if not isinstance(payload, dict):
        raise WeatherError("response was not a JSON object")
    current = payload.get("current")
    if not isinstance(current, dict):
        raise WeatherError("response has no 'current' object")

    temp = current.get("temperature_2m")
    if isinstance(temp, bool) or not isinstance(temp, (int, float)):
        raise WeatherError(f"current.temperature_2m is not a number: {temp!r}")
    temp = float(temp)
    if not math.isfinite(temp):
        raise WeatherError("current.temperature_2m is not finite")

    code = current.get("weather_code")
    if isinstance(code, bool) or not isinstance(code, (int, float)):
        raise WeatherError(f"current.weather_code is not a number: {code!r}")

    # Open-Meteo returns is_day as 1/0 rather than a JSON boolean.
    is_day = current.get("is_day", 1)
    if isinstance(is_day, bool):
        day = is_day
    elif isinstance(is_day, (int, float)):
        day = bool(int(is_day))
    else:
        raise WeatherError(f"current.is_day is not 0 or 1: {is_day!r}")

    return Reading(
        temp_c=round(temp, 1),
        code=int(code),
        is_day=day,
        fetched_at=time.time(),
    )


class WeatherPoller:
    """Fetches current conditions on a timer and hands each one to a callback.

    The callback is called only on a *successful* poll. A failed fetch leaves
    [last] alone, so a device connecting during an internet outage still gets
    the last thing we knew rather than nothing.
    """

    def __init__(
        self,
        latitude: float,
        longitude: float,
        interval_s: float = DEFAULT_POLL_S,
        on_reading: Callable[[Reading], None] | None = None,
        fetch: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.interval_s = max(60.0, float(interval_s))
        self._on_reading = on_reading
        # Injectable so the tests never touch the network.
        self._fetch = fetch or self._http_fetch
        self._last: Reading | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self.failures = 0

    @property
    def last(self) -> Reading | None:
        return self._last

    # --- polling ----------------------------------------------------------

    async def poll_once(self) -> Reading | None:
        """One fetch. Returns the new reading, or None if anything went wrong.

        Catches ``Exception`` deliberately: this runs forever in a background
        task on a home internet connection, and there is no failure here worth
        stopping the server for.
        """
        try:
            payload = await self._fetch()
            reading = parse_current(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failures += 1
            # INFO, not ERROR: with the router rebooting this fires every ten
            # minutes and it is not a fault the household needs to hear about.
            log.info(
                "weather fetch failed (%s); keeping %s",
                exc,
                "the last reading" if self._last else "no reading",
            )
            return None

        self.failures = 0
        self._last = reading
        log.debug(
            "weather: %.1f C, code %d, %s",
            reading.temp_c,
            reading.code,
            "day" if reading.is_day else "night",
        )
        if self._on_reading is not None:
            try:
                self._on_reading(reading)
            except Exception:
                log.exception("weather callback failed")
        return reading

    async def run(self) -> None:
        """Poll immediately, then every ``interval_s`` until cancelled.

        The first poll is not delayed: a server that has just started should
        have something to send the moment the device says hello.
        """
        while True:
            await self.poll_once()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass

    def refresh_soon(self) -> None:
        """Cut the current wait short -- used when a device connects and we have
        nothing cached to send it."""
        self._wake.set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="weather-poller")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # --- transport --------------------------------------------------------

    async def _http_fetch(self) -> Any:
        params = {
            "latitude": f"{self.latitude:.4f}",
            "longitude": f"{self.longitude:.4f}",
            "current": "temperature_2m,weather_code,is_day",
            "timezone": "auto",
        }
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL, params=params) as response:
                response.raise_for_status()
                # Open-Meteo serves application/json; be explicit rather than
                # trusting the content type on a public endpoint.
                return await response.json(content_type=None)
