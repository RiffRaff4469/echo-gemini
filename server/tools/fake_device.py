"""Pretend to be the Echo Show 5, so the server can be built without hardware.

This is HANDOFF verification step 11 -- "drive the server from a desktop script
before involving the device" -- and it is the fastest debugging loop in the
project. It speaks the real protocol over a real socket:

  * connects to /ws with the shared secret and sends ``hello``
  * streams a bundled 16 kHz TTS sample as gated 20 ms PCM16 frames
  * prints every control message the server sends, and every audio frame
  * answers ``ping`` with ``pong``
  * opens its "camera" when the server sends ``video enabled`` -- streaming a
    bundled JPEG at the requested FPS, or reporting the privacy shutter closed
  * writes the model's 24 kHz reply to a WAV so you can listen to it

Examples::

    # display / socket smoke test, no audio, no key needed
    python server/tools/fake_device.py --no-audio

    # full voice loop: tap to talk, stream the sample, save the reply
    python server/tools/fake_device.py --tap --save-reply reply.wav

    # let the wake word in the sample ("Alexa...") trigger the session
    python server/tools/fake_device.py

    # pretend the physical privacy shutter is closed
    python server/tools/fake_device.py --tap --shutter
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import time
import wave
from pathlib import Path

import aiohttp

TOOLS_DIR = Path(__file__).resolve().parent
SERVER_DIR = TOOLS_DIR.parent
REPO_ROOT = SERVER_DIR.parent
sys.path.insert(0, str(SERVER_DIR))

import protocol as P  # noqa: E402

DEFAULT_WAV = TOOLS_DIR / "assets" / "wake-sample.wav"
DEFAULT_JPEG = TOOLS_DIR / "assets" / "test-frame.jpg"

_START = time.monotonic()


def say(text: str) -> None:
    print(f"[{time.monotonic() - _START:7.2f}s] {text}", flush=True)


def load_secret(explicit: str | None) -> str:
    if explicit:
        return explicit
    secret = os.environ.get("ECHO_SHARED_SECRET", "").strip()
    if secret:
        return secret
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ECHO_SHARED_SECRET="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(
        "No shared secret. Pass --secret, set ECHO_SHARED_SECRET, or create .env "
        "from .env.example."
    )


def read_pcm(path: Path) -> bytes:
    """Load a WAV and insist it is exactly what the device would produce."""
    if not path.is_file():
        raise SystemExit(f"WAV not found: {path}")
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (
            P.AUDIO_CHANNELS,
            P.AUDIO_SAMPLE_WIDTH,
            P.AUDIO_UP_RATE,
        ):
            raise SystemExit(
                f"{path.name} is {wav.getframerate()} Hz / {wav.getnchannels()} ch / "
                f"{wav.getsampwidth() * 8}-bit; the device only ever sends "
                f"{P.AUDIO_UP_RATE} Hz mono PCM16.\n"
                f"Convert it:  ffmpeg -i {path.name} -ar 16000 -ac 1 -c:a pcm_s16le out.wav"
            )
        return wav.readframes(wav.getnframes())


class FakeDevice:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.seq = P.SeqCounter()
        self.mic_open = True
        self.state = P.UiState.IDLE
        self.vision: P.Video | None = None
        self.reply_audio = bytearray()
        self.reply_chunks = 0
        self.displays = 0
        self.interrupts = 0
        self.frames_sent = 0
        self._camera_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # --- sending ----------------------------------------------------------

    async def send(self, msg: P.Message) -> None:
        assert self.ws is not None
        await self.ws.send_str(msg.encode())

    async def send_frame(self, channel: P.Channel, payload: bytes) -> None:
        assert self.ws is not None
        await self.ws.send_bytes(
            P.MediaFrame(
                channel=channel, payload=payload, seq=self.seq.next(channel)
            ).encode()
        )

    # --- main loop --------------------------------------------------------

    async def run(self) -> int:
        headers = {"X-Echo-Secret": self.args.secret}
        say(f"connecting to {self.args.url}")
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            try:
                async with http.ws_connect(
                    self.args.url, headers=headers, heartbeat=None
                ) as ws:
                    self.ws = ws
                    say("connected")
                    await self.send(
                        P.Hello(
                            device_id=self.args.device_id,
                            app_version="fake-device/1.0",
                            capabilities={
                                "camera": not self.args.shutter,
                                "microphone": True,
                                "display": "960x480",
                            },
                        )
                    )
                    reader = asyncio.create_task(self._read_loop(), name="reader")
                    sender = asyncio.create_task(self._script(), name="script")
                    done, pending = await asyncio.wait(
                        [reader, sender], return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in done:
                        if task.exception():
                            raise task.exception()  # type: ignore[misc]
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status == 401:
                    say("REJECTED (401): the shared secret does not match the server")
                else:
                    say(f"handshake failed: {exc}")
                return 1
            except aiohttp.ClientConnectorError as exc:
                say(f"cannot reach the server: {exc}")
                say("is `python server/main.py` running?")
                return 1
            finally:
                await self._stop_camera()

        self._report()
        return 0

    async def _read_loop(self) -> None:
        assert self.ws is not None
        async for raw in self.ws:
            if raw.type is aiohttp.WSMsgType.TEXT:
                await self._on_text(raw.data)
            elif raw.type is aiohttp.WSMsgType.BINARY:
                self._on_binary(raw.data)
            elif raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
        say("socket closed by server")
        self._stop.set()

    async def _on_text(self, raw: str) -> None:
        try:
            msg = P.decode(raw)
        except P.ProtocolError as exc:
            say(f"<- UNDECODABLE {exc}: {raw[:160]}")
            return

        if isinstance(msg, P.Welcome):
            say(
                f"<- welcome  protocol=v{msg.protocol_version} "
                f"heartbeat={msg.heartbeat_interval_s:g}s "
                f"audio={msg.audio_up_rate}->{msg.audio_down_rate} Hz "
                f"live={'enabled' if msg.live_enabled else 'DISABLED (no API key)'}"
            )
        elif isinstance(msg, P.Ping):
            say(f"<- ping {msg.nonce}   -> pong")
            await self.send(P.Pong(nonce=msg.nonce))
        elif isinstance(msg, P.StateMsg):
            self.state = msg.state
            say(f"<- state    {msg.state.value.upper()}")
        elif isinstance(msg, P.Mic):
            self.mic_open = msg.enabled
            gain = f" gain={msg.gain:g}" if msg.gain is not None else ""
            say(f"<- mic      {'OPEN' if msg.enabled else 'MUTED (server speaking)'}{gain}")
        elif isinstance(msg, P.Interrupt):
            self.interrupts += 1
            say("<- INTERRUPT -- flushing playback (barge-in)")
        elif isinstance(msg, P.Display):
            self.displays += 1
            cmd = msg.command
            say(
                f"<- display  type={cmd.type.value} duration={cmd.duration:g}s "
                f"priority={cmd.priority} payload={cmd.payload}"
            )
        elif isinstance(msg, P.DisplayClear):
            say("<- display_clear -- back to the ambient clock")
        elif isinstance(msg, P.Video):
            await self._on_video(msg)
        elif isinstance(msg, P.ErrorMsg):
            say(f"<- ERROR    {msg.code}: {msg.message}")
        else:
            say(f"<- {msg.TYPE.value} {msg.fields()}")

    def _on_binary(self, raw: bytes) -> None:
        try:
            frame = P.MediaFrame.decode(raw)
        except P.ProtocolError as exc:
            say(f"<- bad media frame: {exc}")
            return
        if frame.channel is P.Channel.AUDIO_DOWN:
            self.reply_chunks += 1
            self.reply_audio.extend(frame.payload)
            if self.reply_chunks % 25 == 1:
                total = len(self.reply_audio) / (P.AUDIO_DOWN_RATE * 2)
                say(f"<- audio    {len(frame.payload)} bytes ({total:.1f}s of reply so far)")
        else:
            say(f"<- unexpected media on {frame.channel.name}")

    # --- camera -----------------------------------------------------------

    async def _on_video(self, msg: P.Video) -> None:
        if msg.enabled:
            say(
                f"<- video ON  {msg.width}x{msg.height} @{msg.fps:g} FPS q={msg.jpeg_quality} "
                "-- opening camera"
            )
            self.vision = msg
            if self.args.shutter:
                # Patches 0015/0016 keep the camera enumerated with the latch
                # engaged, so a real device gets black frames rather than an
                # error. Reporting it beats streaming black to the model.
                say("-> camera_status shutter_closed (simulated privacy latch)")
                await self.send(
                    P.CameraStatusMsg(
                        status=P.CameraStatus.SHUTTER_CLOSED,
                        detail="frames are near-black; physical shutter engaged",
                    )
                )
                return
            await self.send(P.CameraStatusMsg(status=P.CameraStatus.STREAMING))
            self._camera_task = asyncio.create_task(self._camera_loop(msg))
        else:
            say("<- video OFF -- releasing camera")
            await self._stop_camera()
            self.vision = None
            with contextlib.suppress(Exception):
                await self.send(P.CameraStatusMsg(status=P.CameraStatus.RELEASED))

    async def _camera_loop(self, request: P.Video) -> None:
        jpeg = Path(self.args.jpeg).read_bytes()
        interval = 1.0 / max(request.fps, 0.01)
        try:
            while not self._stop.is_set():
                await self.send_frame(P.Channel.VIDEO_UP, jpeg)
                self.frames_sent += 1
                say(f"-> video frame #{self.frames_sent} ({len(jpeg)} bytes JPEG)")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            say(f"camera loop stopped: {exc}")

    async def _stop_camera(self) -> None:
        if self._camera_task is None:
            return
        self._camera_task.cancel()
        with contextlib.suppress(BaseException):
            await self._camera_task
        self._camera_task = None

    # --- the scripted device behaviour ------------------------------------

    async def _script(self) -> None:
        await asyncio.sleep(0.4)  # let the handshake settle

        if self.args.tap:
            say("-> tap (manual override -- opens a session without the wake word)")
            await self.send(P.Tap(pressed=True))
            await asyncio.sleep(0.3)

        if not self.args.no_audio:
            await self._stream_audio()

        say(f"idling for {self.args.hold:g}s (Ctrl+C to stop early)")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=self.args.hold)

    async def _stream_audio(self) -> None:
        pcm = read_pcm(Path(self.args.wav))
        duration = len(pcm) / (P.AUDIO_UP_RATE * 2)
        passes = self.args.repeat
        say(
            f"-> streaming {Path(self.args.wav).name}: {duration:.1f}s x{passes} "
            f"as {P.AUDIO_FRAME_MS} ms frames"
        )
        frame_bytes = P.AUDIO_UP_FRAME_BYTES
        frame_period = P.AUDIO_FRAME_MS / 1000.0
        suppressed = 0

        for _ in range(passes):
            next_at = time.monotonic()
            for offset in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
                if self._stop.is_set():
                    return
                if self.mic_open:
                    await self.send_frame(
                        P.Channel.AUDIO_UP, pcm[offset : offset + frame_bytes]
                    )
                else:
                    # Half-duplex: the real device stops sending while the
                    # server is speaking, and so does this.
                    suppressed += 1
                next_at += frame_period
                delay = next_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
        say(
            f"-> audio done ({suppressed} frames suppressed while the server was speaking)"
        )

    # --- summary ----------------------------------------------------------

    def _report(self) -> None:
        print()
        say("--- transcript summary ---")
        say(f"display commands received : {self.displays}")
        say(f"interrupts (barge-in)     : {self.interrupts}")
        say(f"video frames sent         : {self.frames_sent}")
        reply_s = len(self.reply_audio) / (P.AUDIO_DOWN_RATE * 2)
        say(f"model audio received      : {reply_s:.1f}s in {self.reply_chunks} chunks")
        if self.reply_audio and self.args.save_reply:
            path = Path(self.args.save_reply)
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(P.AUDIO_CHANNELS)
                wav.setsampwidth(P.AUDIO_SAMPLE_WIDTH)
                wav.setframerate(P.AUDIO_DOWN_RATE)
                wav.writeframes(bytes(self.reply_audio))
            say(f"wrote the model's reply to {path}")
        elif not self.reply_audio:
            say(
                "no model audio -- expected if GEMINI_API_KEY is unset "
                "(the socket and display paths are still proven above)"
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("ECHO_URL", "ws://127.0.0.1:8765/ws"),
        help="server WebSocket URL (default: %(default)s)",
    )
    parser.add_argument("--secret", help="shared secret (default: .env / environment)")
    parser.add_argument("--device-id", default="fake-checkers-01")
    parser.add_argument("--wav", default=str(DEFAULT_WAV), help="16 kHz mono PCM16 WAV")
    parser.add_argument("--jpeg", default=str(DEFAULT_JPEG), help="frame to send as video")
    parser.add_argument(
        "--no-audio", action="store_true", help="connect and idle; no microphone"
    )
    parser.add_argument(
        "--tap",
        action="store_true",
        help="send tap-to-talk instead of waiting for the wake word",
    )
    parser.add_argument("--repeat", type=int, default=1, help="replay the WAV N times")
    parser.add_argument(
        "--hold",
        type=float,
        default=20.0,
        help="seconds to stay connected after the audio ends (default: %(default)s)",
    )
    parser.add_argument(
        "--shutter",
        action="store_true",
        help="simulate the physical privacy shutter being closed",
    )
    parser.add_argument("--save-reply", help="write the model's audio to this WAV")
    return parser.parse_args(argv)


async def amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.secret = load_secret(args.secret)
    return await FakeDevice(args).run()


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:
        say("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
