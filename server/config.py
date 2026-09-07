"""Configuration for echo-server. Everything comes from the environment.

Precedence: real environment variables win over ``.env`` (so a Windows service
environment overrides a developer's local file). ``.env`` is gitignored;
``.env.example`` is the committed template.

**No secret ever has a default value here.** ``GEMINI_API_KEY`` and
``ECHO_SHARED_SECRET`` are read from the environment or not at all.
"""

from __future__ import annotations

import logging
import os
import secrets
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

import spotify_auth

log = logging.getLogger("echo.config")

REPO_ROOT = Path(__file__).resolve().parent.parent

# The Live model line has been renamed repeatedly (HANDOFF section 7); this is
# configuration, not a constant. Check https://ai.google.dev/api/live before
# assuming this default is still current.
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-live-preview"


class ConfigError(RuntimeError):
    """Startup-fatal configuration problem, with a message a human can act on."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass
class Config:
    # --- listener -----------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8765
    shared_secret: str = ""
    heartbeat_interval_s: float = 15.0
    # The WebSocket handshake ALWAYS checks the secret. POST /display can opt
    # out for a trusted script on the same box; it defaults to on.
    display_require_secret: bool = True

    # --- Gemini Live --------------------------------------------------------
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_voice: str = "Puck"
    gemini_media_resolution: str = "MEDIA_RESOLUTION_MEDIUM"
    system_instruction: str = ""
    session_idle_timeout_s: float = 120.0
    session_max_duration_s: float = 600.0
    # Trailing silence after which the user's turn is explicitly closed. The
    # Live API does NOT end a turn on its own for realtime audio input, so
    # without this the model never answers (see gemini_live._pump_end_turn).
    # 0 disables the explicit turn-end.
    session_end_turn_silence_s: float = 1.2
    # How long the session waits after the model has FINISHED answering before
    # it closes itself. This is what makes a quick question cost about ten
    # seconds instead of two minutes, and -- because the live mic stops being
    # streamed the moment the session ends -- it is also what stops the device
    # relaying the rest of the room's conversation to the model. Nothing here
    # can cut an answer short: the window only starts once the model has
    # stopped speaking (see gemini_live._watchdog). 0 disables it, leaving
    # SESSION_IDLE_TIMEOUT as the only close.
    post_answer_silence_s: float = 8.0

    # --- wake word ----------------------------------------------------------
    wake_enabled: bool = True
    wake_model: str = "alexa"
    wake_threshold: float = 0.5
    wake_refractory_s: float = 2.0
    wake_download_models: bool = True

    # --- vision -------------------------------------------------------------
    # off        : never ask for frames
    # on_demand  : ask when a session starts and the user is likely asking about
    #              something visible (server decides; see gemini_live)
    # always     : stream frames for the whole session
    vision_mode: str = "on_demand"
    vision_fps: float = 1.0
    vision_jpeg_quality: int = 80

    # --- alarms and timers --------------------------------------------------
    # How long a Gemini tool call waits for the device to acknowledge an
    # alarm_command. Short on purpose: the model is mid-conversation, and
    # "I could not reach the display" beats ten seconds of dead air.
    alarm_ack_timeout_s: float = 3.0

    # --- ambient weather ----------------------------------------------------
    # Pushed to the device's home screen. The Show has no internet of its own,
    # so this is fetched here (Open-Meteo, no API key) and sent over the link.
    # Defaults are Syracuse, NY.
    weather_enabled: bool = True
    weather_lat: float = 43.0481
    weather_lon: float = -76.1474
    weather_poll_s: float = 600.0

    # --- Spotify ------------------------------------------------------------
    # librespot runs here as a subprocess and appears on the account as a
    # Connect speaker; its PCM is resampled and sent down the device link (see
    # server/spotify.py). Off by default: it needs Premium and a one-time
    # browser login, and neither should be a precondition for the clock.
    spotify_enabled: bool = False
    spotify_device_name: str = "Jarvis"
    spotify_librespot_bin: str = "tools/librespot.exe"
    # Gitignored. Holds the OAuth refresh token and librespot's own credential
    # cache -- treat it exactly as you would the account password.
    spotify_creds_dir: str = "server/spotify_creds"
    spotify_client_id: str = ""
    spotify_redirect_uri: str = ""
    # 96 | 160 | 320 kbps, as librespot accepts them.
    spotify_bitrate: int = 320
    spotify_initial_volume: int = 60
    # Seconds between now-playing polls. Also the granularity of the progress
    # bar on the card, since that is what makes the push worth sending.
    spotify_poll_s: float = 5.0
    spotify_card_priority: int = 0
    # Re-serve album art from this server rather than handing the device a
    # Spotify CDN URL. The Show reaches this machine and, by design, not much
    # else -- the same reason the weather is polled here.
    spotify_art_proxy: bool = True
    spotify_extra_args: list[str] = field(default_factory=list)

    # --- device tuning ------------------------------------------------------
    # Software gain applied on-device before uplink. The real value comes from
    # the Phase 3 mic measurement -- see docs/HARDWARE-STATUS.md.
    mic_gain: float = 1.0

    # --- misc ---------------------------------------------------------------
    log_level: str = "INFO"
    record_audio_dir: str = ""

    warnings: list[str] = field(default_factory=list)

    @property
    def live_enabled(self) -> bool:
        """False means the socket and display paths still work fully; only the
        Gemini leg is inert. This is a supported operating mode, not an error."""
        return bool(self.gemini_api_key)

    def describe(self) -> str:
        return (
            f"listen={self.host}:{self.port} "
            f"live={'on:' + self.gemini_model if self.live_enabled else 'off (no GEMINI_API_KEY)'} "
            f"wake={self.wake_model if self.wake_enabled else 'off'} "
            f"vision={self.vision_mode} "
            f"weather={f'{self.weather_lat:.3f},{self.weather_lon:.3f}' if self.weather_enabled else 'off'} "
            f"spotify={self.spotify_device_name if self.spotify_enabled else 'off'} "
            f"idle_close={self.session_idle_timeout_s:g}s "
            f"quiet_close={self.post_answer_silence_s:g}s"
        )


def load_config(env_file: str | os.PathLike[str] | None = None) -> Config:
    """Build a ``Config``, loading ``.env`` first if present.

    Raises ``ConfigError`` for problems that must stop startup -- chiefly a
    missing shared secret, which would otherwise leave an unauthenticated
    WebSocket listening on the tailnet.
    """
    path = Path(env_file) if env_file else REPO_ROOT / ".env"
    if path.is_file():
        load_dotenv(path, override=False)
        log.info("loaded environment from %s", path)

    cfg = Config(
        host=_env("ECHO_HOST", "0.0.0.0"),
        port=_env_int("ECHO_PORT", 8765),
        shared_secret=os.environ.get("ECHO_SHARED_SECRET", ""),
        heartbeat_interval_s=_env_float("ECHO_HEARTBEAT_INTERVAL_S", 15.0),
        display_require_secret=_env_bool("ECHO_DISPLAY_REQUIRE_SECRET", True),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_model=_env("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
        gemini_voice=_env("GEMINI_VOICE", "Puck"),
        gemini_media_resolution=_env(
            "GEMINI_MEDIA_RESOLUTION", "MEDIA_RESOLUTION_MEDIUM"
        ),
        session_idle_timeout_s=_env_float("SESSION_IDLE_TIMEOUT", 120.0),
        session_max_duration_s=_env_float("SESSION_MAX_DURATION", 600.0),
        session_end_turn_silence_s=_env_float("SESSION_END_TURN_SILENCE_S", 1.2),
        post_answer_silence_s=_env_float("POST_ANSWER_SILENCE_S", 8.0),
        wake_enabled=_env_bool("WAKE_ENABLED", True),
        wake_model=_env("WAKE_MODEL", "alexa"),
        wake_threshold=_env_float("WAKE_THRESHOLD", 0.5),
        wake_refractory_s=_env_float("WAKE_REFRACTORY_S", 2.0),
        wake_download_models=_env_bool("WAKE_DOWNLOAD_MODELS", True),
        vision_mode=_env("VISION_MODE", "on_demand").lower(),
        vision_fps=_env_float("VISION_FPS", 1.0),
        vision_jpeg_quality=_env_int("VISION_JPEG_QUALITY", 80),
        alarm_ack_timeout_s=_env_float("ALARM_ACK_TIMEOUT_S", 3.0),
        weather_enabled=_env_bool("WEATHER_ENABLED", True),
        weather_lat=_env_float("WEATHER_LAT", 43.0481),
        weather_lon=_env_float("WEATHER_LON", -76.1474),
        weather_poll_s=_env_float("WEATHER_POLL_S", 600.0),
        spotify_enabled=_env_bool("SPOTIFY_ENABLED", False),
        spotify_device_name=_env("SPOTIFY_DEVICE_NAME", "Jarvis"),
        spotify_librespot_bin=_env("SPOTIFY_LIBRESPOT_BIN", "tools/librespot.exe"),
        spotify_creds_dir=_env("SPOTIFY_CREDS_DIR", "server/spotify_creds"),
        spotify_client_id=_env("SPOTIFY_CLIENT_ID", spotify_auth.LIBRESPOT_CLIENT_ID),
        spotify_redirect_uri=_env(
            "SPOTIFY_REDIRECT_URI", spotify_auth.DEFAULT_REDIRECT_URI
        ),
        spotify_bitrate=_env_int("SPOTIFY_BITRATE", 320),
        spotify_initial_volume=_env_int("SPOTIFY_INITIAL_VOLUME", 60),
        spotify_poll_s=_env_float("SPOTIFY_POLL_S", 5.0),
        spotify_card_priority=_env_int("SPOTIFY_CARD_PRIORITY", 0),
        spotify_art_proxy=_env_bool("SPOTIFY_ART_PROXY", True),
        spotify_extra_args=shlex.split(_env("SPOTIFY_LIBRESPOT_ARGS", "")),
        mic_gain=_env_float("MIC_GAIN", 1.0),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        record_audio_dir=_env("RECORD_AUDIO_DIR", ""),
    )

    cfg.system_instruction = _load_system_instruction()

    if cfg.vision_mode not in ("off", "on_demand", "always"):
        raise ConfigError(
            f"VISION_MODE must be off|on_demand|always, got {cfg.vision_mode!r}"
        )
    if cfg.vision_fps > 1.0:
        cfg.warnings.append(
            f"VISION_FPS={cfg.vision_fps} exceeds the Live API's 1 FPS cap; clamping to 1.0"
        )
        cfg.vision_fps = 1.0
    if not 0.0 < cfg.alarm_ack_timeout_s <= 30.0:
        raise ConfigError("ALARM_ACK_TIMEOUT_S must be in (0, 30]")
    if not 0.0 < cfg.wake_threshold <= 1.0:
        raise ConfigError("WAKE_THRESHOLD must be in (0, 1]")
    if not -90.0 <= cfg.weather_lat <= 90.0:
        raise ConfigError(f"WEATHER_LAT must be in [-90, 90], got {cfg.weather_lat}")
    if not -180.0 <= cfg.weather_lon <= 180.0:
        raise ConfigError(f"WEATHER_LON must be in [-180, 180], got {cfg.weather_lon}")
    if cfg.weather_poll_s < 60.0:
        # Open-Meteo is free and unauthenticated; polling it harder than once a
        # minute is rude and buys nothing (the data updates hourly).
        cfg.warnings.append(
            f"WEATHER_POLL_S={cfg.weather_poll_s:g} is below the 60 s floor; using 60"
        )
        cfg.weather_poll_s = 60.0

    # Both are resolved against the repo root so a relative value in .env means
    # the same thing whether the server is started from the repo, from the
    # service wrapper, or from somewhere else entirely.
    cfg.spotify_creds_dir = str(_resolve(cfg.spotify_creds_dir))
    cfg.spotify_librespot_bin = str(_resolve(cfg.spotify_librespot_bin))
    if cfg.spotify_enabled:
        if cfg.spotify_bitrate not in (96, 160, 320):
            raise ConfigError(
                f"SPOTIFY_BITRATE must be 96, 160 or 320, got {cfg.spotify_bitrate}"
            )
        if not 0 <= cfg.spotify_initial_volume <= 100:
            raise ConfigError("SPOTIFY_INITIAL_VOLUME must be in [0, 100]")
        if not cfg.spotify_device_name.strip():
            raise ConfigError("SPOTIFY_DEVICE_NAME must not be empty")
        if cfg.spotify_poll_s < 1.0:
            # The card's progress bar is the only thing that needs this, and it
            # is a bar on a bedside display. Polling Spotify harder buys nothing
            # and counts against the account's rate limit.
            cfg.warnings.append(
                f"SPOTIFY_POLL_S={cfg.spotify_poll_s:g} is below the 1 s floor; using 1"
            )
            cfg.spotify_poll_s = 1.0
        if not Path(cfg.spotify_librespot_bin).is_file():
            # Not fatal: the rest of the server, including every other tool,
            # must still come up. Spotify simply reports itself unavailable.
            cfg.warnings.append(
                f"SPOTIFY_ENABLED is set but no librespot binary at "
                f"{cfg.spotify_librespot_bin} -- music will not play. Download "
                "the Windows build or set SPOTIFY_LIBRESPOT_BIN."
            )

    if not cfg.shared_secret:
        raise ConfigError(
            "ECHO_SHARED_SECRET is not set. The WebSocket handshake requires it "
            "(HANDOFF section 7) and there is deliberately no default -- an "
            "unauthenticated listener on the tailnet is exactly what this guards "
            "against.\n"
            "Generate one and put it in .env (copy .env.example first):\n"
            f"    ECHO_SHARED_SECRET={secrets.token_urlsafe(32)}"
        )
    if len(cfg.shared_secret) < 16:
        cfg.warnings.append(
            "ECHO_SHARED_SECRET is shorter than 16 characters; use "
            "`python -c \"import secrets;print(secrets.token_urlsafe(32))\"`"
        )

    if not cfg.live_enabled:
        cfg.warnings.append(
            "GEMINI_API_KEY is not set -- Gemini Live is DISABLED. The device "
            "link, heartbeat, wake word and POST /display all still work."
        )

    return cfg


def _resolve(path_raw: str) -> Path:
    """Interpret a configured path relative to the repo root, not the cwd."""
    path = Path(path_raw).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def _load_system_instruction() -> str:
    """``GEMINI_SYSTEM_INSTRUCTION`` inline, or ``..._FILE`` pointing at a file."""
    inline = os.environ.get("GEMINI_SYSTEM_INSTRUCTION", "").strip()
    if inline:
        return inline
    path_raw = _env("GEMINI_SYSTEM_INSTRUCTION_FILE")
    if not path_raw:
        return (
            "You are a voice assistant living in a small kitchen display. "
            "Answer briefly and conversationally -- one or two sentences unless "
            "asked for more. You may be shown a camera view of the room; "
            "describe what you actually see and say so when you cannot see."
        )
    path = Path(path_raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_file():
        raise ConfigError(f"GEMINI_SYSTEM_INSTRUCTION_FILE not found: {path}")
    return path.read_text(encoding="utf-8").strip()
