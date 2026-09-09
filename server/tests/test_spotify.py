"""Spotify playback against fakes -- no librespot binary, no account, no network.

What is worth pinning down here is not "does the API get called" but the
behaviour that is annoying to discover on real hardware at bedtime:

  * the arbitration rules, which decide whether an album comes back on after a
    conversation, after an alarm, or not at all;
  * that a ``now_playing`` card is pushed for THIS speaker and cleared when the
    music moves to a phone;
  * that librespot's command line carries no token into the log, and that a
    crashed librespot is restarted rather than mourned.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from config import Config
from protocol import DisplayType
from spotify import (
    ALARM_HOLD,
    POLL_BACKOFF_MAX_S,
    POLL_IDLE_S,
    SESSION_HOLD,
    LibrespotSupervisor,
    NowPlaying,
    SpotifyController,
    SpotifyError,
)
from spotify_api import SpotifyApiError


def make_config(**overrides: Any) -> Config:
    cfg = Config(
        shared_secret="x" * 32,
        spotify_enabled=True,
        spotify_device_name="Jarvis",
        spotify_poll_s=5.0,
        spotify_art_proxy=True,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


TRACK = {
    "name": "Weightless",
    "id": "trk1",
    "duration_ms": 480_000,
    "artists": [{"name": "Marconi Union"}],
    "album": {
        "name": "Ambient Transmissions",
        "images": [
            {"url": "https://i.scdn.co/big", "width": 640},
            {"url": "https://i.scdn.co/mid", "width": 300},
            {"url": "https://i.scdn.co/small", "width": 64},
        ],
    },
}


class FakeApi:
    """The slice of ``WebApi`` the controller uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.device: dict | None = {"id": "dev-1", "name": "Jarvis"}
        self.results: list[dict] = [{"uri": "spotify:track:trk1", **TRACK}]
        self.state: dict | None = None
        self.fail_with: Exception | None = None
        self.closed = False

    def _note(self, _call: str, **kwargs: Any) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append((_call, kwargs))

    def named(self, name: str) -> list[dict]:
        return [args for call, args in self.calls if call == name]

    async def find_device(self, name: str):
        self._note("find_device", name=name)
        return self.device

    async def search(self, query: str, *, kind: str = "track", limit: int = 5):
        self._note("search", query=query, kind=kind)
        return list(self.results)

    async def play(self, *, device_id, uris=None, context_uri=""):
        self._note("play", device_id=device_id, uris=uris, context_uri=context_uri)

    async def resume(self, *, device_id):
        self._note("resume", device_id=device_id)

    async def pause(self, *, device_id=""):
        self._note("pause", device_id=device_id)

    async def next_track(self, *, device_id=""):
        self._note("next", device_id=device_id)

    async def previous_track(self, *, device_id=""):
        self._note("previous", device_id=device_id)

    async def set_volume(self, percent, *, device_id=""):
        self._note("volume", percent=percent, device_id=device_id)

    async def playback_state(self):
        self._note("state")
        return self.state

    async def access_token(self, *, force: bool = False) -> str:
        return "tok"

    async def aclose(self) -> None:
        self.closed = True


class Recorder:
    """Stands in for the hub: collects PCM, cards and clears."""

    def __init__(self) -> None:
        self.pcm: list[bytes] = []
        self.cards: list = []
        self.clears = 0

    def on_pcm(self, pcm: bytes) -> None:
        self.pcm.append(pcm)

    def push(self, cmd) -> None:
        self.cards.append(cmd)

    def clear(self) -> None:
        self.clears += 1


class FakeSupervisor:
    running = True
    restarts = 0
    last_error = ""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def build(cfg: Config | None = None, *, state: dict | None = None):
    cfg = cfg or make_config()
    api = FakeApi()
    api.state = state
    sink = Recorder()
    controller = SpotifyController(
        cfg,
        on_pcm=sink.on_pcm,
        push_card=sink.push,
        clear_card=sink.clear,
        api=api,  # type: ignore[arg-type]
        supervisor=FakeSupervisor(),  # type: ignore[arg-type]
    )
    # Stand in for a completed one-time browser login.
    controller.tokens.refresh_token = "refresh"
    controller.device_attached("http://100.64.0.1:8765")
    return controller, api, sink


def playing_state(*, is_playing: bool = True, device: str = "Jarvis", progress_ms=1000):
    return {
        "item": dict(TRACK),
        "is_playing": is_playing,
        "progress_ms": progress_ms,
        "device": {"id": "dev-1", "name": device},
    }


# --- parsing ----------------------------------------------------------------


def test_now_playing_parses_a_track():
    now = NowPlaying.from_state(playing_state())
    assert now is not None
    assert now.title == "Weightless"
    assert now.artist == "Marconi Union"
    assert now.album == "Ambient Transmissions"
    assert now.duration_s == 480.0
    assert now.is_playing is True
    assert now.summary() == "Playing Weightless by Marconi Union."


def test_now_playing_picks_the_art_the_card_actually_draws():
    """640 px is four times the bytes for a 200 px slot; 64 px is visibly soft."""
    now = NowPlaying.from_state(playing_state())
    assert now is not None and now.art_url == "https://i.scdn.co/mid"


@pytest.mark.parametrize("state", [None, {}, {"item": None}, {"item": {"name": ""}}])
def test_now_playing_handles_nothing_playing(state):
    assert NowPlaying.from_state(state) is None


def test_paused_reads_as_paused():
    now = NowPlaying.from_state(playing_state(is_playing=False))
    assert now is not None and now.summary().startswith("Paused ")


# --- the tool surface -------------------------------------------------------


async def test_play_searches_then_starts_on_our_device():
    controller, api, sink = build(state=playing_state())
    result = await controller.play("weightless", "track")

    assert result["ok"] is True
    assert result["summary"] == "Playing Weightless by Marconi Union."
    assert api.named("search")[0]["query"] == "weightless"
    started = api.named("play")[0]
    assert started["device_id"] == "dev-1"
    assert started["uris"] == ["spotify:track:trk1"]


async def test_play_a_playlist_uses_a_context_not_a_track_list():
    """'Play some lofi' means an hour of it, which is a context_uri."""
    controller, api, _ = build(state=playing_state())
    controller_results = [{"uri": "spotify:playlist:lofi", "name": "lofi beats"}]
    api.results = controller_results

    await controller.play("lofi", "playlist")

    started = api.named("play")[0]
    assert started["context_uri"] == "spotify:playlist:lofi"
    assert not started["uris"]


async def test_play_with_no_results_says_so_rather_than_playing_nothing():
    controller, api, _ = build()
    api.results = []
    with pytest.raises(SpotifyError, match="could not find"):
        await controller.play("asdfghjkl")


async def test_play_needs_the_speaker_to_be_visible():
    controller, api, _ = build()
    api.device = None
    with pytest.raises(SpotifyError, match="not showing up"):
        await controller.play("anything")


async def test_play_without_a_link_refuses_early():
    controller, api, _ = build()
    controller.tokens.refresh_token = ""
    with pytest.raises(SpotifyError, match="not linked"):
        await controller.play("anything")
    assert not api.calls


@pytest.mark.parametrize(
    "method,call,summary",
    [
        ("pause", "pause", "Paused."),
        ("next_track", "next", "Playing Weightless by Marconi Union."),
        ("previous_track", "previous", "Playing Weightless by Marconi Union."),
    ],
)
async def test_transport_controls(method, call, summary):
    controller, api, _ = build(state=playing_state())
    result = await getattr(controller, method)()
    assert result["summary"] == summary
    assert api.named(call)


async def test_volume_is_clamped_to_a_sane_request():
    controller, api, _ = build()
    assert (await controller.set_volume(40))["summary"] == "Volume 40."
    assert api.named("volume")[0]["percent"] == 40


@pytest.mark.parametrize("bad", [-1, 101, "loud", None])
async def test_volume_rejects_nonsense(bad):
    controller, _, _ = build()
    with pytest.raises(SpotifyError):
        await controller.set_volume(bad)


async def test_status_reads_the_player_rather_than_remembering():
    controller, api, _ = build(state=playing_state())
    result = await controller.status()
    assert result["title"] == "Weightless"
    assert api.named("state")


async def test_status_with_nothing_playing():
    controller, _, _ = build(state=None)
    assert (await controller.status())["summary"] == "Nothing is playing."


# --- the now-playing card ---------------------------------------------------


async def test_playing_pushes_a_now_playing_card():
    controller, _, sink = build(state=playing_state())
    await controller.play("weightless")

    assert len(sink.cards) == 1
    card = sink.cards[0]
    assert card.type is DisplayType.NOW_PLAYING
    assert card.duration == 0  # stays up until it is replaced or cleared
    assert card.payload["title"] == "Weightless"
    assert card.payload["is_playing"] is True


async def test_album_art_is_proxied_through_this_server():
    """The Show reaches this machine and, by design, very little else."""
    controller, _, sink = build(state=playing_state())
    await controller.play("weightless")
    assert sink.cards[0].payload["art_url"] == "http://100.64.0.1:8765/spotify/art/trk1"


async def test_art_proxy_can_be_turned_off():
    controller, _, sink = build(make_config(spotify_art_proxy=False), state=playing_state())
    await controller.play("weightless")
    assert sink.cards[0].payload["art_url"] == "https://i.scdn.co/mid"


async def test_an_unchanged_paused_track_stops_re_pushing():
    """A paused track's progress does not move, so nothing needs re-sending."""
    controller, _, sink = build(state=playing_state(is_playing=False, progress_ms=1000))
    await controller._refresh()
    await controller._refresh()
    await controller._refresh()
    assert len(sink.cards) == 1


async def test_progress_moving_re_pushes_so_the_bar_advances():
    controller, api, sink = build(state=playing_state(progress_ms=1_000))
    await controller._refresh()
    api.state = playing_state(progress_ms=30_000)
    await controller._refresh()
    assert len(sink.cards) == 2


async def test_music_on_another_device_clears_the_card():
    """Music the owner started on their phone is playing -- but not here, and a
    card claiming otherwise says the display is doing something it is not."""
    controller, api, sink = build(state=playing_state())
    await controller._refresh()
    assert sink.cards and sink.clears == 0

    api.state = playing_state(device="Pixel")
    await controller._refresh()
    assert sink.clears == 1


async def test_playback_stopping_clears_the_card():
    controller, api, sink = build(state=playing_state())
    await controller._refresh()
    api.state = None
    await controller._refresh()
    assert sink.clears == 1


# --- poll cadence -----------------------------------------------------------


async def test_a_timeout_does_not_slow_the_poll():
    """Spotify missing one answer is a blip it recovers from on its own.
    Backing off for it would make the card stale over a failure that was never
    about how often we asked."""
    controller, api, _ = build(state=playing_state())
    await controller._refresh()
    assert controller._poll_delay() == 5.0

    api.fail_with = SpotifyApiError("Spotify did not answer in time.")
    await controller._refresh()
    assert controller._poll_delay() == 5.0


async def test_a_429_backs_the_poll_off_until_spotify_answers_again():
    """A rate limit is the one failure caused by the polling itself, so it is
    the one that has to slow down -- and it must climb to a cap rather than
    away, and come back to the base once reads get through."""
    controller, api, _ = build(state=playing_state())
    await controller._refresh()

    api.fail_with = SpotifyApiError("rate-limited", status=429)
    delays = []
    for _ in range(8):
        await controller._refresh()
        delays.append(controller._poll_delay())

    assert delays[:3] == [10.0, 20.0, 40.0]
    assert delays[-1] == POLL_BACKOFF_MAX_S
    assert max(delays) == POLL_BACKOFF_MAX_S, "backoff is not capped"

    api.fail_with = None
    await controller._refresh()
    assert controller._poll_delay() == 5.0


async def test_nothing_playing_polls_lazily():
    """No card on screen and no bar to advance: five seconds is spending a
    request budget on an answer that cannot have changed usefully."""
    controller, _, _ = build(state=None)
    await controller._refresh()
    assert controller._poll_delay() == POLL_IDLE_S


# --- audio ------------------------------------------------------------------


def test_pcm_reaches_the_device_when_nothing_holds_the_speaker():
    controller, _, sink = build()
    controller._emit_pcm(b"\x01\x02")
    assert sink.pcm == [b"\x01\x02"]


async def test_a_second_toggle_press_does_not_re_pause():
    """The touch row's toggle asks this. ``audio_flowing`` alone lags a pause by
    over a second -- long enough for a second press to pause it again rather
    than resume."""
    controller, _, _ = build(state=playing_state())
    controller._emit_pcm(b"audio")
    await controller._refresh()
    assert controller.is_playing is True

    await controller.pause()
    assert controller.is_playing is False, "still 'playing' -> toggle pauses twice"


async def test_the_card_shows_paused_immediately_not_one_poll_later():
    """Spotify keeps reporting `is_playing` for a beat after a pause. The card
    must follow what the user just did, not what the API has caught up to."""
    controller, api, sink = build(state=playing_state())
    await controller._refresh()
    assert sink.cards[-1].payload["is_playing"] is True

    await controller.pause()  # api.state still says playing, as Spotify would
    assert sink.cards[-1].payload["is_playing"] is False


async def test_pcm_is_dropped_while_held_rather_than_queued():
    """Music buffered through a two-minute conversation would arrive as two
    minutes of stale music afterwards."""
    controller, _, sink = build()
    await controller.hold(SESSION_HOLD)
    controller._emit_pcm(b"\x01\x02")
    assert sink.pcm == []
    await controller.release(SESSION_HOLD)
    controller._emit_pcm(b"\x03\x04")
    assert sink.pcm == [b"\x03\x04"]


# --- arbitration ------------------------------------------------------------


async def test_a_session_pauses_playing_music_and_resumes_it():
    controller, api, _ = build(state=playing_state())
    controller._emit_pcm(b"audio")  # the stream is live right now

    await controller.hold(SESSION_HOLD)
    assert api.named("pause")

    await controller.release(SESSION_HOLD)
    assert api.named("resume")


async def test_a_session_does_not_start_music_that_was_not_playing():
    controller, api, _ = build(state=None)
    await controller.hold(SESSION_HOLD)
    await controller.release(SESSION_HOLD)
    assert not api.named("pause")
    assert not api.named("resume")


async def test_an_alarm_during_a_conversation_holds_the_music_past_the_session():
    """Two holds, one speaker: the music must not come back while the alarm is
    still ringing just because the conversation ended."""
    controller, api, _ = build(state=playing_state())
    controller._emit_pcm(b"audio")

    await controller.hold(SESSION_HOLD)
    await controller.hold(ALARM_HOLD)
    await controller.release(SESSION_HOLD)
    assert not api.named("resume")

    await controller.release(ALARM_HOLD)
    assert api.named("resume")


async def test_asking_for_music_mid_conversation_beats_the_session_hold():
    """Otherwise 'play some lofi' answers with eight seconds of silence, then
    music -- the request having been honoured but muted until the session ends."""
    controller, _, sink = build(state=playing_state())
    await controller.hold(SESSION_HOLD)

    await controller.play("lofi")

    controller._emit_pcm(b"music")
    assert sink.pcm == [b"music"]


async def test_saying_pause_outlives_the_conversation():
    """The session remembered music was playing when the wake word fired. It
    must not use that to undo an explicit 'pause' eight seconds later."""
    controller, api, _ = build(state=playing_state())
    controller._emit_pcm(b"audio")
    await controller.hold(SESSION_HOLD)

    await controller.pause()
    await controller.release(SESSION_HOLD)

    assert not api.named("resume")


async def test_a_hold_survives_spotify_being_unreachable():
    """A hold that cannot pause still mutes the stream, so the conversation is
    audible either way -- and must not raise into the session lifecycle."""
    controller, api, sink = build(state=playing_state())
    controller._emit_pcm(b"audio")
    api.fail_with = SpotifyApiError("network gone")

    await controller.hold(SESSION_HOLD)
    controller._emit_pcm(b"more")
    assert sink.pcm == [b"audio"]


async def test_an_unreleased_hold_expires():
    """A device that drops off Wi-Fi mid-alarm never reports the dismissal, and
    music paused forever is a bug nobody would think to look for."""
    ticks = [0.0]
    controller, api, _ = build(state=playing_state())
    controller._clock = lambda: ticks[0]
    controller._last_pcm_at = 0.0

    await controller.hold(ALARM_HOLD, max_hold_s=600.0)
    assert api.named("pause")

    ticks[0] = 601.0
    await controller._expire_holds()
    assert api.named("resume")


# --- librespot --------------------------------------------------------------


def test_argv_carries_the_flags_the_pipeline_depends_on():
    sup = _supervisor()
    argv = sup.argv("secret-token")
    assert "--backend" in argv and argv[argv.index("--backend") + 1] == "pipe"
    assert "--format" in argv and argv[argv.index("--format") + 1] == "S16"
    assert argv[argv.index("--name") + 1] == "Jarvis"
    assert argv[argv.index("--access-token") + 1] == "secret-token"


def test_extra_args_are_appended_so_a_flag_rename_is_recoverable():
    sup = _supervisor(extra_args=["--zeroconf-port", "9999"])
    assert sup.argv("t")[-2:] == ["--zeroconf-port", "9999"]


def test_the_token_never_reaches_the_log():
    from spotify import _redact

    assert "secret-token" not in " ".join(_redact(_supervisor().argv("secret-token")))


async def test_a_missing_binary_is_reported_not_crashed():
    sup = _supervisor(binary="does-not-exist.exe")
    with pytest.raises(SpotifyError, match="librespot binary"):
        await sup.start()


async def test_pcm_flows_from_the_pipe_through_the_converter():
    import struct

    frames = b"".join(struct.pack("<hh", 1000, 1000) for _ in range(4_410))
    proc = FakeProcess(frames)
    sup = _supervisor(popen=lambda *a, **k: proc)
    got: list[bytes] = []
    sup._on_pcm = got.append

    await sup.start()
    await proc.finished.wait()
    await asyncio.sleep(0.05)
    await sup.stop()

    assert got, "no converted audio reached the sink"
    assert len(b"".join(got)) < len(frames)  # 44.1 kHz stereo -> 24 kHz mono


async def test_the_pipe_is_drained_at_real_time_not_at_decode_speed():
    """The reader must pace itself or a track is burnt in seconds (owner
    choppy-audio report): 8 chunks of 50 ms each = 400 ms of audio, and the
    fake pipe hands it over instantly, the way a decoder far ahead of real
    time does. Draining at 1x must spread the sink deliveries over roughly
    that wall time instead of finishing in a few milliseconds."""
    import struct
    import time

    chunk = b"".join(struct.pack("<hh", 1000, 1000) for _ in range(4_410))
    expected = 8
    proc = FakeProcess(chunk * expected)
    sup = _supervisor(popen=lambda *a, **k: proc)
    got: list[bytes] = []
    sup._on_pcm = got.append

    t0 = time.monotonic()
    await sup.start()
    for _ in range(400):  # 8 s ceiling; normally ~0.4 s
        if len(got) >= expected:
            break
        await asyncio.sleep(0.02)
    elapsed = time.monotonic() - t0
    await sup.stop()

    assert len(got) >= expected, f"only {len(got)} of {expected} chunks drained"
    # Every 50 ms chunk yields ~4_800 bytes of 24 kHz mono. The generous
    # bounds absorb Windows timer-quantum jitter and a loaded machine; the
    # point is the order of magnitude -- hundreds of ms, not tens.
    assert 0.15 <= elapsed <= 4.0, f"drain took {elapsed:.2f}s for 0.4s of audio"


async def test_a_crashed_librespot_is_restarted():
    procs: list[FakeProcess] = []

    def spawn(*_a, **_k):
        proc = FakeProcess(b"", exit_code=1)
        procs.append(proc)
        return proc

    sup = _supervisor(popen=spawn)
    await sup.start()
    for _ in range(200):
        if len(procs) >= 2:
            break
        await asyncio.sleep(0.02)
    await sup.stop()

    assert len(procs) >= 2
    assert sup.restarts >= 1


async def test_stop_leaves_no_process_behind():
    proc = FakeProcess(b"", block_forever=True)
    sup = _supervisor(popen=lambda *a, **k: proc)
    await sup.start()
    await asyncio.sleep(0.05)
    await sup.stop()
    assert proc.terminated


# --- fakes ------------------------------------------------------------------


def _supervisor(*, binary: str | None = None, popen=None, extra_args=None, tmp=None):
    from pathlib import Path

    return LibrespotSupervisor(
        binary=binary or __file__,  # any file that exists; never executed
        device_name="Jarvis",
        cache_dir=Path(__file__).resolve().parent / "_creds_should_not_be_used",
        bitrate=320,
        initial_volume=60,
        extra_args=extra_args,
        token_provider=_token,
        on_pcm=lambda _pcm: None,
        popen=popen or (lambda *a, **k: FakeProcess(b"")),
    )


async def _token() -> str:
    return "tok"


class FakeProcess:
    """The slice of ``subprocess.Popen`` the supervisor touches.

    ``stdout`` hands over the whole payload once and then reports EOF, which is
    what a real pipe does when librespot exits.
    """

    def __init__(self, payload: bytes, *, exit_code: int = 0, block_forever: bool = False):
        self.payload = payload
        self.exit_code = exit_code
        self.block_forever = block_forever
        self.terminated = False
        self.finished = asyncio.Event()
        self._loop = asyncio.get_event_loop()
        self._done = __import__("threading").Event()
        self.stdout = _Pipe(payload)
        self.stderr = _Pipe(b"")

    def poll(self):
        return None if not self._done.is_set() else self.exit_code

    def wait(self, timeout=None):
        if self.block_forever:
            self._done.wait(timeout if timeout is not None else 30)
            if not self._done.is_set():
                raise TimeoutError
        else:
            self._done.set()
        self._loop.call_soon_threadsafe(self.finished.set)
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self._done.set()
        self.stdout.close()

    def kill(self):
        self.terminate()


class _Pipe:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0
        self._closed = False

    def read(self, size: int) -> bytes:
        if self._closed or self._offset >= len(self._payload):
            return b""
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def readline(self) -> bytes:
        return b""

    def close(self) -> None:
        self._closed = True
