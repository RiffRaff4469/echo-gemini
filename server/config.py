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
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

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
            f"idle_close={self.session_idle_timeout_s:g}s"
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
        wake_enabled=_env_bool("WAKE_ENABLED", True),
        wake_model=_env("WAKE_MODEL", "alexa"),
        wake_threshold=_env_float("WAKE_THRESHOLD", 0.5),
        wake_refractory_s=_env_float("WAKE_REFRACTORY_S", 2.0),
        wake_download_models=_env_bool("WAKE_DOWNLOAD_MODELS", True),
        vision_mode=_env("VISION_MODE", "on_demand").lower(),
        vision_fps=_env_float("VISION_FPS", 1.0),
        vision_jpeg_quality=_env_int("VISION_JPEG_QUALITY", 80),
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
    if not 0.0 < cfg.wake_threshold <= 1.0:
        raise ConfigError("WAKE_THRESHOLD must be in (0, 1]")

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
