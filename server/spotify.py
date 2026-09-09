"""Spotify playback: librespot as a speaker, the Web API as the remote control.

The shape of this, and why (docs/SPOTIFY.md):

    Spotify  --Connect-->  librespot.exe  --stdout PCM-->  PcmConverter
                                                              |
                                       24 kHz mono PCM16 <----+
                                                              |
                                        Channel.AUDIO_MUSIC --+--> the Show

The Show stays a thin speaker and display. No Spotify client, no DRM, no Google
services on a 32-bit device with under a gigabyte of RAM -- it receives PCM on a
second audio channel and plays it, which is the one thing it is already good at.

**librespot cannot be told anything.** It is a Connect *receiver*: it registers
a device called "Jarvis" and plays what the account sends it. Search, play,
pause, skip, volume and "what is playing" are all Spotify Web API calls aimed at
that device -- see ``spotify_api.py``. Which is also why controlling the music
from a phone works without any code here: the phone is talking to the same
Connect device.

## Arbitration (v1, deliberately blunt)

One speaker, three things that want it, resolved by holds rather than mixing:

  * a **voice session** takes a hold while it is open. Music pauses when the
    wake word fires and resumes afterwards if -- and only if -- it was playing
    before. Ducking instead was rejected: the device's microphone is weak
    enough that music at any level under it ruins recognition (HANDOFF 8.2 is
    half-duplex for the same reason).
  * an **alarm or timer** takes a hold while it is ringing. The alarm wins
    outright; it is the one sound in the house with a deadline.
  * everything else plays.

The limiter to know about: a hold *pauses* Spotify, so a hold taken while a
podcast is playing loses nothing, but a hold taken during a live radio stream
resumes at the live edge rather than where it stopped. That is Spotify's
behaviour, not something this file can fix.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from protocol import AUDIO_DOWN_RATE, DisplayCommand, DisplayType
from resample import LIBRESPOT_CHANNELS, LIBRESPOT_RATE, PcmConverter
from spotify_api import SpotifyApiError, SpotifyNotLinked, WebApi
from spotify_auth import TokenStore

log = logging.getLogger("echo.spotify")

# 50 ms of 44.1 kHz stereo PCM16. Small enough that a pause is heard promptly,
# large enough that the reader thread is not woken a thousand times a second.
PIPE_READ_BYTES = 8_820

# Real-time drain target for the pipe, in bytes of 44.1 kHz stereo PCM16 per
# second. librespot's pipe backend does NOT pace itself: StdoutSink writes as
# fast as the decoder produces, so an undrained pipe lets it burn a whole
# track in seconds (and flood the device link -- the root cause of the
# 2026-09-08 choppy-audio report). Draining at ~1x keeps the OS pipe full and
# librespot's writer blocked on it, and that backpressure is the pacemaker.
PIPE_BYTES_PER_S = LIBRESPOT_RATE * LIBRESPOT_CHANNELS * 2  # 176_400

# How long after the last PCM chunk music is still considered to be playing.
# Used to answer "was it playing before this session started?" without a Web API
# round trip on the wake-word path.
PCM_LIVENESS_S = 1.5

# librespot restart backoff. Capped low: this is a local subprocess, and the
# common failure is an expired token, which the next attempt fixes.
RESTART_BACKOFF_S = (1.0, 2.0, 5.0, 15.0, 30.0)

# Album art is fetched once per track and served back to the device from this
# server (see ``ArtCache``). A handful of entries covers an album's worth of
# skipping back and forth.
ART_CACHE_MAX = 16
ART_TARGET_EDGE = 300  # the card renders it at ~200 px on a 960x480 panel

# The ceiling on the now-playing poll while Spotify is rate-limiting us. A 429
# is the one failure that polling harder makes worse, so the interval doubles
# per 429 and stops here: two minutes is stale, but it is a card, and the
# alternative is an account-wide throttle that breaks the controls too.
POLL_BACKOFF_MAX_S = 120.0

# The floor on the poll when nothing is playing. The card is not on screen and
# the progress bar is not moving, so asking every five seconds spends a
# request-per-second budget on an answer that cannot have changed usefully.
POLL_IDLE_S = 30.0

# How often the rate-limit warning may repeat. Without this a throttled hour
# writes six hundred identical lines and buries whatever else went wrong.
RATE_LIMIT_LOG_S = 60.0

# How long an explicit control overrides what the player state reports.
# Spotify's ``GET /me/player`` lags a pause or a resume by a beat, so the read
# that immediately follows a control can still describe the state we just
# changed -- which would push a now-playing card contradicting what the user
# just did, and leave the toggle button pausing something already paused.
CONTROL_INTENT_S = 3.0

# The two things that take the speaker away from the music. Named because the
# holds interact: asking for music *during* a conversation has to beat the
# conversation's own hold, and must not touch the alarm's.
SESSION_HOLD = "session"
ALARM_HOLD = "alarm"

# An alarm hold is released when the device reports nothing ringing. A device
# that drops off Wi-Fi mid-alarm never reports, so the hold gets an expiry --
# music silently paused forever is a bug nobody would think to look for.
ALARM_HOLD_MAX_S = 600.0


class SpotifyError(RuntimeError):
    """Anything the model should say out loud as one plain sentence."""


class SpotifyDisabled(SpotifyError):
    pass


# Both failure families read the same to a caller: one sentence to say out
# loud. They are separate classes because they are raised in separate layers
# (transport versus feature), and this tuple is what the tool handlers catch so
# neither can escape as a traceback into a live conversation.
MUSIC_ERRORS = (SpotifyError, SpotifyApiError)


# ---------------------------------------------------------------------------
# What is playing
# ---------------------------------------------------------------------------


@dataclass
class NowPlaying:
    """One snapshot of the player, in the shape the ``now_playing`` card wants."""

    title: str = ""
    artist: str = ""
    album: str = ""
    art_url: str = ""
    progress_s: float = 0.0
    duration_s: float = 0.0
    is_playing: bool = False
    device_name: str = ""
    device_id: str = ""
    item_id: str = ""

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"title": self.title, "is_playing": self.is_playing}
        if self.artist:
            out["artist"] = self.artist
        if self.album:
            out["album"] = self.album
        if self.art_url:
            out["art_url"] = self.art_url
        if self.duration_s > 0:
            out["duration_s"] = round(self.duration_s, 1)
            out["progress_s"] = round(max(0.0, self.progress_s), 1)
        return out

    def summary(self) -> str:
        """One sentence, for a voice model to read back verbatim."""
        if not self.title:
            return "Nothing is playing."
        who = f" by {self.artist}" if self.artist else ""
        verb = "Playing" if self.is_playing else "Paused"
        return f"{verb} {self.title}{who}."

    @staticmethod
    def from_state(state: dict[str, Any] | None) -> "NowPlaying | None":
        """Parse ``GET /v1/me/player``. Returns None when there is no track.

        A podcast episode, an advert or a local file can all leave ``item``
        null or without an album; every field beyond the title is optional here
        for that reason.
        """
        if not isinstance(state, dict):
            return None
        item = state.get("item")
        if not isinstance(item, dict) or not item.get("name"):
            return None

        album = item.get("album") if isinstance(item.get("album"), dict) else {}
        artists = item.get("artists") or []
        device = state.get("device") if isinstance(state.get("device"), dict) else {}

        return NowPlaying(
            title=str(item.get("name") or ""),
            artist=", ".join(
                str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name")
            ),
            album=str((album or {}).get("name") or ""),
            art_url=_pick_art((album or {}).get("images")),
            progress_s=float(state.get("progress_ms") or 0) / 1000.0,
            duration_s=float(item.get("duration_ms") or 0) / 1000.0,
            is_playing=bool(state.get("is_playing")),
            device_name=str((device or {}).get("name") or ""),
            device_id=str((device or {}).get("id") or ""),
            item_id=str(item.get("id") or item.get("uri") or ""),
        )


def _pick_art(images: Any) -> str:
    """The image closest to the size the card actually draws.

    Spotify offers 640, 300 and 64 px. Sending the 640 costs four times the
    bytes for pixels a 200 px slot throws away, and the 64 is visibly soft.
    """
    if not isinstance(images, list):
        return ""
    best, best_gap = "", None
    for image in images:
        if not isinstance(image, dict) or not image.get("url"):
            continue
        gap = abs(int(image.get("width") or 0) - ART_TARGET_EDGE)
        if best_gap is None or gap < best_gap:
            best, best_gap = str(image["url"]), gap
    return best


# ---------------------------------------------------------------------------
# librespot
# ---------------------------------------------------------------------------


class LibrespotSupervisor:
    """Runs ``librespot.exe``, reads its PCM, and restarts it when it dies.

    Three threads' worth of blocking work is kept off the event loop: the
    stdout pipe read, the format conversion (see ``resample``), and the stderr
    drain. Converted audio crosses back with ``call_soon_threadsafe``, which is
    the only safe way in.

    ``popen`` is injectable so the tests can drive the whole lifecycle -- spawn,
    audio, crash, restart, shutdown -- against a fake, with no binary present.
    """

    def __init__(
        self,
        *,
        binary: str | Path,
        device_name: str,
        cache_dir: Path,
        bitrate: int,
        initial_volume: int,
        extra_args: list[str] | None,
        token_provider: Callable[[], Any],
        on_pcm: Callable[[bytes], None],
        popen: Callable[..., Any] = subprocess.Popen,
        converter_factory: Callable[[], PcmConverter] | None = None,
    ) -> None:
        self.binary = Path(binary)
        self.device_name = device_name
        self.cache_dir = Path(cache_dir)
        self.bitrate = bitrate
        self.initial_volume = initial_volume
        self.extra_args = list(extra_args or [])
        self._token_provider = token_provider
        self._on_pcm = on_pcm
        self._popen = popen
        self._converter_factory = converter_factory or (
            lambda: PcmConverter(
                in_rate=LIBRESPOT_RATE,
                in_channels=LIBRESPOT_CHANNELS,
                out_rate=AUDIO_DOWN_RATE,
            )
        )

        self._task: asyncio.Task | None = None
        self._proc: Any = None
        self._stopping = False
        self.restarts = 0
        self.last_error = ""

    # --- state ------------------------------------------------------------

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def argv(self, token: str) -> list[str]:
        """The command line, in one place so a flag change is one edit.

        Flags are librespot 0.6's. Verify them against the actual Windows build
        before trusting this end to end -- upstream renames options between
        minor releases, and ``--backend pipe`` in particular has to be confirmed
        to emit PCM on stdout rather than needing ``--device``.
        """
        args = [
            str(self.binary),
            "--name",
            self.device_name,
            "--device-type",
            "speaker",
            "--backend",
            "pipe",
            # 44.1 kHz stereo S16 is what ``resample`` is built to consume.
            "--format",
            "S16",
            "--bitrate",
            str(self.bitrate),
            "--initial-volume",
            str(self.initial_volume),
            # Credentials cache. The audio cache is off deliberately: this
            # machine is not short of disk, but a music cache is a pile of
            # decrypted-adjacent data nobody asked for.
            "--cache",
            str(self.cache_dir),
            "--disable-audio-cache",
        ]
        if token:
            args += ["--access-token", token]
        return args + self.extra_args

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not self.binary.is_file():
            raise SpotifyError(
                f"The librespot binary is not at {self.binary}. Download the "
                "Windows build and set SPOTIFY_LIBRESPOT_BIN."
            )
        self._stopping = False
        self._task = asyncio.create_task(self._supervise(), name="librespot")

    async def stop(self) -> None:
        self._stopping = True
        # Terminating can block for the grace period, so it goes to a thread:
        # this runs from aiohttp's cleanup hook, where stalling the loop stalls
        # the whole shutdown.
        await asyncio.get_running_loop().run_in_executor(None, self._terminate)
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=8.0)
            except asyncio.TimeoutError:
                log.warning("librespot supervisor did not stop in 8 s; cancelling")
                task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("librespot supervisor raised while stopping")

    def _terminate(self) -> None:
        """Ask the process to go, then insist. Never leave one behind.

        A librespot left running holds the Connect device name, so the next
        start registers a second "Jarvis" and the Web API play call picks the
        wrong one -- a hung subprocess here is not a leak, it is a bug the owner
        sees.
        """
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            with_kill = getattr(proc, "kill", None)
            if with_kill is not None:
                try:
                    with_kill()
                except Exception:
                    log.exception("could not kill librespot")

    async def _supervise(self) -> None:
        loop = asyncio.get_running_loop()
        attempt = 0
        while not self._stopping:
            try:
                token = await self._token_provider()
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("librespot cannot start without a token: %s", exc)
                token = ""

            try:
                proc = self._spawn(token)
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("could not spawn librespot")
                proc = None

            if proc is not None:
                self._proc = proc
                converter = self._converter_factory()
                started = time.monotonic()
                self._start_thread(self._read_audio, (proc, converter, loop), "pcm")
                self._start_thread(self._read_errors, (proc,), "log")
                code = await loop.run_in_executor(None, proc.wait)
                self._proc = None
                if self._stopping:
                    log.info("librespot exited on shutdown (code %s)", code)
                    return
                self.restarts += 1
                self.last_error = f"librespot exited with code {code}"
                log.warning("librespot exited with code %s; restarting", code)
                # A process that played for a while and then died is a
                # different failure from one that will not start, and must not
                # inherit the other's backoff: an evening of music should not
                # come back from a blip with a 30 s silence.
                if time.monotonic() - started >= 30.0:
                    attempt = 0

            delay = RESTART_BACKOFF_S[min(attempt, len(RESTART_BACKOFF_S) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    def _spawn(self, token: str) -> Any:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        argv = self.argv(token)
        log.info("starting librespot as %r", self.device_name)
        log.debug("librespot argv: %s", " ".join(_redact(argv)))
        kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            "bufsize": 0,
        }
        if sys.platform == "win32":
            # Without this a console window flashes up on a machine somebody is
            # actually using.
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return self._popen(argv, **kwargs)

    @staticmethod
    def _start_thread(target: Callable[..., None], args: tuple, name: str) -> None:
        threading.Thread(
            target=target, args=args, name=f"librespot-{name}", daemon=True
        ).start()

    def _read_audio(self, proc: Any, converter: PcmConverter, loop: Any) -> None:
        """Pipe -> resampler -> event loop. Runs on its own thread, forever.

        Paced to real time: after each chunk the thread sleeps for the wall
        time that chunk of audio represents, so the average drain rate is 1x
        no matter how far ahead librespot's decoder is. The sleep is bounded
        by a deadline that also absorbs the read's own blocking time -- when
        librespot refills slowly the read waits and the deadline falls behind,
        so the sleep is skipped and nothing accumulates.
        """
        stream = proc.stdout
        if stream is None:
            return
        deadline = 0.0
        try:
            while True:
                chunk = stream.read(PIPE_READ_BYTES)
                if not chunk:
                    return
                pcm = converter.feed(chunk)
                if pcm:
                    loop.call_soon_threadsafe(self._on_pcm, pcm)
                # A partial read (Windows returns what the pipe has, not what
                # was asked for) simply represents less audio; the accounting
                # is in bytes read, so it stays exact either way.
                now = time.monotonic()
                deadline = max(deadline, now) + len(chunk) / PIPE_BYTES_PER_S
                delay = deadline - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        except (ValueError, OSError):
            return  # the pipe closed under us: the process is gone
        except Exception:
            log.exception("librespot audio reader failed")

    @staticmethod
    def _read_errors(proc: Any) -> None:
        """Drain stderr into the log.

        Not optional: with ``--backend pipe`` stdout is audio, so stderr is the
        only place librespot can say "bad credentials", and an undrained pipe
        eventually blocks the process that is writing to it.
        """
        stream = proc.stderr
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    log.info("[librespot] %s", line)
        except (ValueError, OSError):
            return
        except Exception:
            log.exception("librespot log reader failed")


def _redact(argv: list[str]) -> list[str]:
    """Keep the access token out of the log file."""
    out = list(argv)
    for i, item in enumerate(out):
        if item == "--access-token" and i + 1 < len(out):
            out[i + 1] = "<token>"
    return out


# ---------------------------------------------------------------------------
# Album art
# ---------------------------------------------------------------------------


class ArtCache:
    """Album art, fetched once per track and re-served from this machine.

    The device reaches this server and, by design, very little else -- the
    weather is polled here for exactly that reason (``server/weather.py``).
    Handing the panel an ``i.scdn.co`` URL and hoping is how you get a card with
    a hole in it, so the art is proxied by default and ``SPOTIFY_ART_PROXY=false``
    turns that off for a device that really can reach the internet.
    """

    def __init__(self, api: WebApi) -> None:
        self._api = api
        self._entries: dict[str, tuple[str, bytes]] = {}
        self._sources: dict[str, str] = {}

    def remember(self, key: str, url: str) -> None:
        if key and url:
            self._sources[key] = url

    async def fetch(self, key: str) -> tuple[str, bytes] | None:
        cached = self._entries.get(key)
        if cached is not None:
            return cached
        url = self._sources.get(key)
        if not url:
            return None
        try:
            http = await self._api.http()
            async with http.get(url) as resp:
                if resp.status != 200:
                    log.info("album art %s returned %d", url, resp.status)
                    return None
                body = await resp.read()
                mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        except Exception as exc:
            log.info("could not fetch album art: %s", exc)
            return None

        if len(self._entries) >= ART_CACHE_MAX:
            self._entries.pop(next(iter(self._entries)), None)
        self._entries[key] = (mime, body)
        return self._entries[key]


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------


@dataclass
class _Hold:
    reason: str
    expires_at: float = 0.0


class SpotifyController:
    """Everything the rest of the server talks to: tools, arbitration, cards.

    Constructed only when ``SPOTIFY_ENABLED``; ``Hub.music`` is None otherwise,
    and every caller treats that as "this display has no music", the same way
    the alarm tools treat a sink with no coordinator.
    """

    def __init__(
        self,
        cfg: Any,
        *,
        on_pcm: Callable[[bytes], None],
        push_card: Callable[[DisplayCommand], None],
        clear_card: Callable[[], None],
        on_card_change: Callable[[bool], None] | None = None,
        api: WebApi | None = None,
        supervisor: LibrespotSupervisor | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self._on_pcm = on_pcm
        self._push_card = push_card
        self._clear_card = clear_card
        self._on_card_change = on_card_change
        self._clock = clock

        self.tokens = TokenStore(
            Path(cfg.spotify_creds_dir) / "token.json", client_id=cfg.spotify_client_id
        )
        self.tokens.load()
        # The Web API can ride its own Spotify app (personal developer app =
        # real rate budget) while librespot's streaming session keeps the
        # librespot credentials. Falls back to the shared store when no
        # separate app is configured.
        if cfg.spotify_api_client_id:
            api_tokens = TokenStore(
                Path(cfg.spotify_api_creds_dir) / "token.json",
                client_id=cfg.spotify_api_client_id,
            )
            api_tokens.load()
        else:
            api_tokens = self.tokens
        self.api = api or WebApi(api_tokens)
        # librespot's own session must keep the librespot-app credentials (a
        # personal-app token is rejected by its spirc login), so it refreshes
        # from the main store via its own WebApi handle used only for tokens.
        self._spawn_tokens = WebApi(self.tokens)
        self.art = ArtCache(self.api)

        self.supervisor = supervisor or LibrespotSupervisor(
            binary=cfg.spotify_librespot_bin,
            device_name=cfg.spotify_device_name,
            cache_dir=Path(cfg.spotify_creds_dir) / "librespot",
            bitrate=cfg.spotify_bitrate,
            initial_volume=cfg.spotify_initial_volume,
            extra_args=cfg.spotify_extra_args,
            token_provider=self._librespot_token,
            on_pcm=self._emit_pcm,
            converter_factory=None,
        )

        self._poll_task: asyncio.Task | None = None
        self._poll_base_s = max(1.0, float(cfg.spotify_poll_s))
        self._poll_interval = self._poll_base_s
        self._rate_limited = False
        self._rate_limit_logged_at = float("-inf")
        self._holds: dict[str, _Hold] = {}
        self._resume_on_release = False
        self._arbitration_lock = asyncio.Lock()

        self._device_id = ""
        self._last_pcm_at = float("-inf")
        self._now: NowPlaying | None = None
        self._intent_playing: bool | None = None
        self._intent_until = 0.0
        self._card_fingerprint: tuple | None = None
        self._card_showing = False
        # Set by the hub at device handshake: the address the DEVICE used to
        # reach this server, which is the only one it is known to be able to
        # resolve. Album art URLs are built from it.
        self.origin = ""
        self.started = False

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self.started:
            return
        if not self.tokens.linked:
            log.warning("%s", self.tokens.how_to_link())
            return
        missing = self.tokens.missing_scopes()
        if missing:
            log.warning(
                "the saved Spotify login is missing scopes %s; log in again if "
                "playback control misbehaves",
                ", ".join(missing),
            )
        self.started = True
        try:
            await self.supervisor.start()
        except SpotifyError as exc:
            self.started = False
            log.error("%s", exc)
            return
        self._poll_task = asyncio.create_task(self._poll_loop(), name="spotify-poll")
        log.info(
            "Spotify enabled: librespot will appear as %r", self.cfg.spotify_device_name
        )

    async def stop(self) -> None:
        self.started = False
        task, self._poll_task = self._poll_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        await self.supervisor.stop()
        await self.api.aclose()

    def device_attached(self, origin: str) -> None:
        """A device has (re)connected at ``origin``, e.g. ``http://100.x.y.z:8765``.

        Two things follow. Album art URLs are built from the address the DEVICE
        used to reach this server, which is the only one it is known to be able
        to resolve -- the server cannot derive it, and guessing produces a card
        with a hole in it. And the card bookkeeping is reset, because a fresh
        device is showing the clock however sure this end was that a card was up.
        """
        self.origin = origin.rstrip("/")
        self._card_fingerprint = None
        self._card_showing = False

    # --- audio ------------------------------------------------------------

    def _emit_pcm(self, pcm: bytes) -> None:
        """Converted music, on the event loop thread. Dropped while held.

        Dropping rather than buffering is right: a hold means something else
        owns the speaker, and audio kept back during a two-minute conversation
        would arrive as two minutes of stale music afterwards.
        """
        self._last_pcm_at = self._clock()
        if self._holds:
            return
        self._on_pcm(pcm)

    @property
    def audio_flowing(self) -> bool:
        return self._clock() - self._last_pcm_at < PCM_LIVENESS_S

    @property
    def is_playing(self) -> bool:
        """Best available answer to "is music playing right now?".

        The player state wins when there is one, because it is updated the
        instant a control is applied -- ``audio_flowing`` lags a pause by up to
        ``PCM_LIVENESS_S``, which is long enough for a second press of the
        toggle button to pause something that is already paused.

        The PCM stream is the fallback, and it is a good one: it is exact, free,
        and available without a Web API round trip, which is what makes it the
        right thing to ask on the wake-word path.
        """
        if self._now is not None and self._now.title:
            return self._now.is_playing
        return self.audio_flowing

    # --- token ------------------------------------------------------------

    async def _librespot_token(self) -> str:
        """A token for librespot's command line, or "" to run cache-only.

        librespot authenticates once and caches long-lived credentials in
        ``spotify_creds/librespot/credentials.json``. Once that cache exists it
        MUST run without ``--access-token``: a token-authed session is
        force-closed by Spotify's AP the moment it tries to stream audio (the
        token belongs to a web-API login, not a device credential), while the
        cached credential streams fine. The token is only the bootstrap for a
        fresh install.
        """
        cache = Path(self.cfg.spotify_creds_dir) / "librespot" / "credentials.json"
        if cache.exists():
            return ""
        return await self._spawn_tokens.access_token()

    # --- device -----------------------------------------------------------

    async def _resolve_device(self, *, wait_s: float = 0.0) -> str:
        """The Connect device id for our librespot, looked up by name.

        Re-resolved rather than cached across restarts: librespot mints a new
        device id every time it starts, and a stale id makes ``play`` fail with
        Spotify's least helpful 404.
        """
        deadline = self._clock() + wait_s
        while True:
            device = await self.api.find_device(self.cfg.spotify_device_name)
            if device is not None:
                self._device_id = str(device.get("id") or "")
                return self._device_id
            if self._clock() >= deadline:
                break
            await asyncio.sleep(0.5)

        self._device_id = ""
        raise SpotifyError(
            f"The speaker {self.cfg.spotify_device_name!r} is not showing up in "
            "Spotify right now."
        )

    # --- the tool surface -------------------------------------------------

    def _require_ready(self) -> None:
        if not self.cfg.spotify_enabled:
            raise SpotifyDisabled("Spotify is switched off on this server.")
        if not self.tokens.linked:
            raise SpotifyError(
                "Spotify is not linked to an account yet, so I cannot play anything."
            )

    async def play(self, query: str, kind: str = "track") -> dict[str, Any]:
        """Search and start playing on the Show.

        ``kind`` is the model's call, and it is the difference between "play
        Blue Monday" (a track) and "play some lofi" (a playlist). Getting it
        wrong is recoverable -- the user says "no, the album" -- so this does
        not try to outguess it.
        """
        self._require_ready()
        query = (query or "").strip()
        if not query:
            raise SpotifyError("I need to know what to play.")

        # librespot can take a moment to register after a restart; waiting a
        # few seconds here beats telling the user it is not there when it is
        # about to be.
        device_id = await self._resolve_device(wait_s=5.0)

        results = await self._search(query, kind)
        if not results:
            raise SpotifyError(f"I could not find anything on Spotify for {query}.")
        chosen = results[0]
        uri = str(chosen.get("uri"))

        if kind == "track":
            await self.api.play(device_id=device_id, uris=[uri])
        else:
            await self.api.play(device_id=device_id, context_uri=uri)
        self._intend(True)

        # Asking for music mid-conversation outranks the conversation's own
        # hold. Without this the request is honoured on Spotify's side and then
        # muted here until the session closes, so "play some lofi" answers with
        # eight seconds of silence and then music -- which reads as broken.
        # The remembered "it was playing before" goes with it: this IS what is
        # playing now, and resuming the old track over it would be wrong.
        self._resume_on_release = False
        await self.release(SESSION_HOLD)

        # Spotify's player state lags the play call by a beat; one retry is the
        # difference between confirming the real track and confirming nothing.
        now = await self._refresh(retries=3, delay_s=0.4)
        summary = now.summary() if now is not None else f"Playing {_label(chosen, kind)}."
        log.info("spotify play(%s, kind=%s) -> %s", query, kind, summary)
        return {"ok": True, "summary": summary}

    async def _search(self, query: str, kind: str) -> list[dict[str, Any]]:
        try:
            return await self.api.search(query, kind=kind, limit=5)
        except SpotifyApiError:
            raise
        except Exception as exc:  # pragma: no cover -- defensive
            raise SpotifyError(f"The Spotify search failed: {exc}") from exc

    async def pause(self) -> dict[str, Any]:
        self._require_ready()
        await self.api.pause(device_id=self._device_id)
        self._mark_paused()
        # "Pause" said out loud has to outlive the conversation. Otherwise the
        # session's own hold resumes the music eight seconds later, having
        # remembered that it was playing when the wake word fired.
        self._resume_on_release = False
        await self._refresh(retries=2, delay_s=0.3)
        return {"ok": True, "summary": "Paused."}

    async def resume(self) -> dict[str, Any]:
        self._require_ready()
        device_id = self._device_id or await self._resolve_device(wait_s=2.0)
        await self.api.resume(device_id=device_id)
        self._intend(True)
        # As for play(): the user asked for music now, so the conversation's
        # hold stops applying and there is nothing left to restore afterwards.
        self._resume_on_release = False
        await self.release(SESSION_HOLD)
        now = await self._refresh(retries=2, delay_s=0.3)
        return {"ok": True, "summary": now.summary() if now else "Playing again."}

    async def next_track(self) -> dict[str, Any]:
        self._require_ready()
        await self.api.next_track(device_id=self._device_id)
        now = await self._refresh(retries=3, delay_s=0.4)
        return {"ok": True, "summary": now.summary() if now else "Skipped."}

    async def previous_track(self) -> dict[str, Any]:
        self._require_ready()
        await self.api.previous_track(device_id=self._device_id)
        now = await self._refresh(retries=3, delay_s=0.4)
        return {"ok": True, "summary": now.summary() if now else "Went back."}

    async def set_volume(self, percent: Any) -> dict[str, Any]:
        self._require_ready()
        try:
            level = int(round(float(percent)))
        except (TypeError, ValueError) as exc:
            raise SpotifyError("Volume has to be a number from 0 to 100.") from exc
        if not 0 <= level <= 100:
            raise SpotifyError("Volume has to be between 0 and 100.")
        device_id = self._device_id or await self._resolve_device(wait_s=2.0)
        await self.api.set_volume(level, device_id=device_id)
        return {"ok": True, "summary": f"Volume {level}."}

    async def status(self) -> dict[str, Any]:
        self._require_ready()
        now = await self._refresh(retries=1, delay_s=0.0)
        if now is None:
            return {"ok": True, "summary": "Nothing is playing."}
        out: dict[str, Any] = {"ok": True, "summary": now.summary()}
        out.update(
            {
                "title": now.title,
                "artist": now.artist,
                "album": now.album,
                "is_playing": now.is_playing,
                "device": now.device_name,
            }
        )
        return out

    # --- arbitration ------------------------------------------------------

    async def hold(self, reason: str, *, max_hold_s: float = 0.0) -> None:
        """Take the speaker away from the music until ``release``.

        Idempotent per reason and reference-counted across reasons, so an alarm
        going off during a conversation does not resume music when only one of
        the two ends.
        """
        async with self._arbitration_lock:
            first = not self._holds
            self._holds[reason] = _Hold(
                reason, self._clock() + max_hold_s if max_hold_s > 0 else 0.0
            )
            if not first:
                return
            # Was there anything to interrupt? The PCM stream answers this
            # instantly and correctly; asking Spotify would add a round trip to
            # the wake-word path for the same answer.
            self._resume_on_release = self.audio_flowing or bool(
                self._now and self._now.is_playing
            )
            if not self._resume_on_release:
                return
            log.info("music pauses for %s", reason)
            try:
                await self.api.pause(device_id=self._device_id)
                self._mark_paused()
            except SpotifyApiError as exc:
                # A hold that cannot pause still mutes the stream, so the
                # conversation is audible either way.
                log.warning("could not pause Spotify for %s: %s", reason, exc)

    async def release(self, reason: str) -> None:
        async with self._arbitration_lock:
            self._holds.pop(reason, None)
            if self._holds or not self._resume_on_release:
                return
            self._resume_on_release = False
            log.info("music resumes after %s", reason)
            try:
                device_id = self._device_id or await self._resolve_device(wait_s=2.0)
                await self.api.resume(device_id=device_id)
                self._intend(True)
            except (SpotifyApiError, SpotifyError) as exc:
                log.warning("could not resume Spotify after %s: %s", reason, exc)

    async def _expire_holds(self) -> None:
        """Drop holds whose owner never released them.

        The alarm hold is the one this exists for: it is released when the
        device reports nothing ringing, and a device that drops off Wi-Fi mid
        alarm would otherwise leave the music paused indefinitely.
        """
        now = self._clock()
        stale = [
            hold.reason
            for hold in self._holds.values()
            if hold.expires_at and now >= hold.expires_at
        ]
        for reason in stale:
            log.warning("hold %r expired without being released", reason)
            await self.release(reason)

    # --- what we just asked for -------------------------------------------

    def _intend(self, playing: bool) -> None:
        """Record that a control was just applied, and believe it for a moment.

        Only pause, resume and play set this. A skip does not: it changes the
        track, not whether one is playing, and Spotify reports the new track's
        state correctly.
        """
        self._intent_playing = playing
        self._intent_until = self._clock() + CONTROL_INTENT_S

    def _apply_intent(self, now: NowPlaying | None) -> NowPlaying | None:
        if now is None or self._intent_playing is None:
            return now
        if self._clock() >= self._intent_until:
            self._intent_playing = None
            return now
        now.is_playing = self._intent_playing
        return now

    def _mark_paused(self) -> None:
        self._intend(False)
        if self._now is not None:
            self._now.is_playing = False

    # --- now playing ------------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._poll_delay())
                await self._expire_holds()
                await self._refresh(retries=1, delay_s=0.0)
            except asyncio.CancelledError:
                raise
            except SpotifyApiError as exc:
                log.info("now-playing poll failed: %s", exc)
            except Exception:
                log.exception("now-playing poll crashed; continuing")

    def _poll_delay(self) -> float:
        """How long to wait before reading the player state again.

        Three cadences in one number. Five seconds while a track is actually
        moving, because the progress bar has to advance. A lazy
        ``POLL_IDLE_S`` when nothing is playing, where the only thing being
        watched for is playback starting somewhere else. And, above both, the
        rate-limit backoff -- that one wins outright, because it is the case
        where the polling itself is the problem.
        """
        if self._poll_interval > self._poll_base_s:
            return self._poll_interval
        if self.is_playing:
            return self._poll_base_s
        return max(self._poll_base_s, POLL_IDLE_S)

    def _note_read_failed(self, exc: SpotifyApiError) -> None:
        """Back off, but only for the failure that backing off actually fixes.

        A timeout or a 5xx is Spotify having a moment; slowing the poll for one
        makes the card stale over something that was never about how often we
        asked. A 429 is the opposite -- it is caused by asking.
        """
        if getattr(exc, "status", 0) != 429:
            log.info("could not read the Spotify player state: %s", exc)
            return

        self._poll_interval = min(self._poll_interval * 2.0, POLL_BACKOFF_MAX_S)
        now = self._clock()
        if not self._rate_limited or now - self._rate_limit_logged_at >= RATE_LIMIT_LOG_S:
            log.warning(
                "Spotify is rate-limiting this server; polling every %.0f s",
                self._poll_interval,
            )
            self._rate_limit_logged_at = now
        self._rate_limited = True

    def _note_read_ok(self) -> None:
        """A read got through, so whatever we backed off from is over."""
        if self._rate_limited:
            log.info(
                "Spotify is answering again; polling every %.0f s", self._poll_base_s
            )
            self._rate_limited = False
        self._poll_interval = self._poll_base_s

    async def _refresh(self, *, retries: int = 1, delay_s: float = 0.0) -> NowPlaying | None:
        """Read the player state and push a card if what it says has changed."""
        now: NowPlaying | None = None
        for attempt in range(max(1, retries)):
            if attempt and delay_s:
                await asyncio.sleep(delay_s)
            try:
                state = await self.api.playback_state()
            except SpotifyNotLinked:
                return None
            except SpotifyApiError as exc:
                self._note_read_failed(exc)
                return self._now
            self._note_read_ok()
            now = self._apply_intent(NowPlaying.from_state(state))
            if now is not None:
                break

        self._now = now
        await self._publish(now)
        return now

    async def _publish(self, now: NowPlaying | None) -> None:
        """Push, replace or clear the ``now_playing`` card.

        Only our own speaker is shown. Music the owner started on their phone,
        in another room, is genuinely playing -- but putting it on the bedside
        display would say the display is playing it, which it is not.
        """
        ours = now is not None and (
            not now.device_name
            or now.device_name.strip().lower()
            == self.cfg.spotify_device_name.strip().lower()
        )
        if now is None or not ours:
            if self._card_showing:
                self._card_showing = False
                self._card_fingerprint = None
                self._clear_card()
                self._note_card(False)
            return

        # Progress is bucketed so a playing track re-pushes about once per poll
        # (the bar has to move) while a paused one goes quiet entirely.
        fingerprint = (
            now.item_id,
            now.is_playing,
            int(now.progress_s // max(1.0, float(self.cfg.spotify_poll_s))),
        )
        if fingerprint == self._card_fingerprint:
            return
        self._card_fingerprint = fingerprint

        payload = now.payload()
        art = await self._art_url(now)
        if art:
            payload["art_url"] = art
        elif "art_url" in payload:
            payload.pop("art_url")

        self._card_showing = True
        self._push_card(
            DisplayCommand(
                type=DisplayType.NOW_PLAYING,
                payload=payload,
                duration=0.0,  # until it is replaced or playback stops
                priority=self.cfg.spotify_card_priority,
            )
        )
        self._note_card(True)

    def _note_card(self, active: bool) -> None:
        """Notify the host that the display's speaker card appeared/cleared
        (used for the focus-driven layout, UI-BRIEF-14)."""
        if self._on_card_change is not None:
            try:
                self._on_card_change(active)
            except Exception:
                log.exception("card-change hook failed")

    async def _art_url(self, now: NowPlaying) -> str:
        if not now.art_url:
            return ""
        if not self.cfg.spotify_art_proxy:
            return now.art_url
        if not now.item_id or not self.origin:
            return now.art_url
        self.art.remember(now.item_id, now.art_url)
        return f"{self.origin}/spotify/art/{now.item_id}"

    # --- diagnostics ------------------------------------------------------

    def info(self) -> dict[str, Any]:
        now = self._now
        return {
            "enabled": bool(self.cfg.spotify_enabled),
            "linked": self.tokens.linked,
            "device_name": self.cfg.spotify_device_name,
            "librespot_running": self.supervisor.running,
            "librespot_restarts": self.supervisor.restarts,
            "last_error": self.supervisor.last_error or None,
            "holds": sorted(self._holds),
            "audio_flowing": self.audio_flowing,
            "now_playing": (
                {
                    "title": now.title,
                    "artist": now.artist,
                    "is_playing": now.is_playing,
                }
                if now is not None and now.title
                else None
            ),
        }


def _label(item: dict[str, Any], kind: str) -> str:
    """A spoken name for a search result, for the case where the player state
    has not caught up yet and there is nothing better to confirm."""
    name = str(item.get("name") or "something")
    artists = item.get("artists") or []
    who = ", ".join(
        str(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name")
    )
    if kind == "artist":
        return name
    return f"{name} by {who}" if who else name
