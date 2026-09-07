"""Config loading -- chiefly the rules that keep secrets out of the repo."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import DEFAULT_GEMINI_MODEL, Config, ConfigError, load_config


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_env():
    """Snapshot and restore the whole environment around each test.

    ``load_dotenv`` writes into ``os.environ``, and it will not override a name
    that is already there -- so without this, one test's .env silently decides
    the next one's config.
    """
    import os

    saved = os.environ.copy()
    for name in list(os.environ):
        if name.startswith(
            (
                "ECHO_",
                "GEMINI_",
                "WAKE_",
                "VISION_",
                "SESSION_",
                "POST_ANSWER_",
                "MIC_",
                "WEATHER_",
                "SPOTIFY_",
            )
        ):
            del os.environ[name]
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_missing_shared_secret_is_fatal(tmp_path) -> None:
    """An unauthenticated WebSocket on the tailnet is exactly what the secret
    guards against, so there is deliberately no default."""
    with pytest.raises(ConfigError) as exc:
        load_config(write_env(tmp_path, "ECHO_PORT=9000\n"))
    assert "ECHO_SHARED_SECRET" in str(exc.value)
    assert "generate" in str(exc.value).lower()


def test_loads_from_env_file(tmp_path) -> None:
    cfg = load_config(
        write_env(
            tmp_path,
            "ECHO_SHARED_SECRET=a-long-enough-test-secret\n"
            "ECHO_PORT=9999\n"
            "SESSION_IDLE_TIMEOUT=45\n"
            "VISION_MODE=always\n",
        )
    )
    assert cfg.port == 9999
    assert cfg.session_idle_timeout_s == 45.0
    assert cfg.vision_mode == "always"
    assert cfg.gemini_model == DEFAULT_GEMINI_MODEL


def test_real_environment_overrides_the_env_file(tmp_path, monkeypatch) -> None:
    """The Windows service environment must win over a developer's local file."""
    monkeypatch.setenv("ECHO_SHARED_SECRET", "from-the-service-environment")
    cfg = load_config(write_env(tmp_path, "ECHO_SHARED_SECRET=from-the-file\n"))
    assert cfg.shared_secret == "from-the-service-environment"


def test_no_api_key_disables_live_but_is_not_an_error(tmp_path) -> None:
    cfg = load_config(write_env(tmp_path, "ECHO_SHARED_SECRET=a-long-enough-secret\n"))
    assert cfg.live_enabled is False
    assert any("GEMINI_API_KEY" in w for w in cfg.warnings)


def test_api_key_enables_live(tmp_path) -> None:
    cfg = load_config(
        write_env(
            tmp_path,
            "ECHO_SHARED_SECRET=a-long-enough-secret\nGEMINI_API_KEY=not-a-real-key\n",
        )
    )
    assert cfg.live_enabled is True


def test_short_secret_warns_but_starts(tmp_path) -> None:
    cfg = load_config(write_env(tmp_path, "ECHO_SHARED_SECRET=short\n"))
    assert any("shorter than 16" in w for w in cfg.warnings)


def test_vision_fps_above_the_api_cap_is_clamped(tmp_path) -> None:
    cfg = load_config(
        write_env(tmp_path, "ECHO_SHARED_SECRET=a-long-enough-secret\nVISION_FPS=30\n")
    )
    assert cfg.vision_fps == 1.0
    assert any("1 FPS" in w for w in cfg.warnings)


def test_bad_vision_mode_is_fatal(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            write_env(
                tmp_path, "ECHO_SHARED_SECRET=a-long-enough-secret\nVISION_MODE=maybe\n"
            )
        )


def test_bad_wake_threshold_is_fatal(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            write_env(
                tmp_path,
                "ECHO_SHARED_SECRET=a-long-enough-secret\nWAKE_THRESHOLD=1.5\n",
            )
        )


def test_non_numeric_value_is_fatal(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            write_env(
                tmp_path,
                "ECHO_SHARED_SECRET=a-long-enough-secret\nSESSION_IDLE_TIMEOUT=soon\n",
            )
        )


def test_post_answer_window_defaults_to_a_few_seconds(tmp_path) -> None:
    """The default has to be short enough that walking away after a quick
    question actually ends the session, and long enough to ask a follow-up."""
    cfg = load_config(write_env(tmp_path, "ECHO_SHARED_SECRET=a-long-enough-secret\n"))
    assert 3.0 <= cfg.post_answer_silence_s <= 15.0
    assert cfg.post_answer_silence_s < cfg.session_idle_timeout_s


def test_post_answer_window_from_the_env_file(tmp_path) -> None:
    cfg = load_config(
        write_env(
            tmp_path,
            "ECHO_SHARED_SECRET=a-long-enough-secret\nPOST_ANSWER_SILENCE_S=0\n",
        )
    )
    assert cfg.post_answer_silence_s == 0.0


def test_weather_defaults_to_syracuse(tmp_path) -> None:
    cfg = load_config(write_env(tmp_path, "ECHO_SHARED_SECRET=a-long-enough-secret\n"))
    assert cfg.weather_enabled is True
    assert (round(cfg.weather_lat, 4), round(cfg.weather_lon, 4)) == (43.0481, -76.1474)
    assert cfg.weather_poll_s == 600.0


def test_weather_location_from_the_env_file(tmp_path) -> None:
    cfg = load_config(
        write_env(
            tmp_path,
            "ECHO_SHARED_SECRET=a-long-enough-secret\n"
            "WEATHER_LAT=51.5072\nWEATHER_LON=-0.1276\nWEATHER_POLL_S=900\n",
        )
    )
    assert (cfg.weather_lat, cfg.weather_lon) == (51.5072, -0.1276)
    assert cfg.weather_poll_s == 900.0


def test_out_of_range_weather_latitude_is_fatal(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            write_env(
                tmp_path, "ECHO_SHARED_SECRET=a-long-enough-secret\nWEATHER_LAT=91\n"
            )
        )


def test_too_frequent_weather_poll_is_clamped_not_fatal(tmp_path) -> None:
    """Open-Meteo is free and unauthenticated; hammering it is our problem to
    prevent, but it is not worth refusing to start over."""
    cfg = load_config(
        write_env(
            tmp_path, "ECHO_SHARED_SECRET=a-long-enough-secret\nWEATHER_POLL_S=5\n"
        )
    )
    assert cfg.weather_poll_s == 60.0
    assert any("WEATHER_POLL_S" in w for w in cfg.warnings)


SECRET = "ECHO_SHARED_SECRET=a-long-enough-secret\n"


def test_spotify_is_off_by_default(tmp_path) -> None:
    """It needs Premium and a one-time browser login, so nothing about it may
    happen on a server nobody has asked to play music."""
    cfg = load_config(write_env(tmp_path, SECRET))
    assert cfg.spotify_enabled is False
    assert cfg.spotify_device_name == "Jarvis"
    assert cfg.spotify_client_id, "the default is librespot's public client id"


def test_spotify_paths_resolve_against_the_repo_root(tmp_path) -> None:
    """A relative value in .env must mean the same thing whichever directory
    the server was started from -- the service wrapper's is not the repo."""
    cfg = load_config(write_env(tmp_path, SECRET))
    assert Path(cfg.spotify_creds_dir).is_absolute()
    assert Path(cfg.spotify_librespot_bin).is_absolute()
    assert Path(cfg.spotify_librespot_bin).name == "librespot.exe"


def test_a_missing_librespot_binary_warns_but_starts(tmp_path) -> None:
    """The rest of the server -- alarms, weather, the whole voice path -- must
    still come up on a machine where the binary was never fetched."""
    cfg = load_config(
        write_env(
            tmp_path,
            SECRET + "SPOTIFY_ENABLED=true\n"
            f"SPOTIFY_LIBRESPOT_BIN={tmp_path / 'nope.exe'}\n",
        )
    )
    assert cfg.spotify_enabled is True
    assert any("no librespot binary" in w for w in cfg.warnings)


def test_bad_spotify_bitrate_is_fatal(tmp_path) -> None:
    """librespot accepts exactly three; anything else makes it exit at spawn,
    which reads as "music silently never starts"."""
    with pytest.raises(ConfigError) as exc:
        load_config(
            write_env(tmp_path, SECRET + "SPOTIFY_ENABLED=true\nSPOTIFY_BITRATE=256\n")
        )
    assert "SPOTIFY_BITRATE" in str(exc.value)


def test_out_of_range_spotify_volume_is_fatal(tmp_path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(
            write_env(
                tmp_path, SECRET + "SPOTIFY_ENABLED=true\nSPOTIFY_INITIAL_VOLUME=140\n"
            )
        )
    assert "SPOTIFY_INITIAL_VOLUME" in str(exc.value)


def test_empty_spotify_device_name_is_fatal(tmp_path) -> None:
    """The device is found by name, so a blank one makes every play call fail
    with Spotify's least helpful 404."""
    with pytest.raises(ConfigError) as exc:
        load_config(
            write_env(
                tmp_path, SECRET + 'SPOTIFY_ENABLED=true\nSPOTIFY_DEVICE_NAME="  "\n'
            )
        )
    assert "SPOTIFY_DEVICE_NAME" in str(exc.value)


def test_too_frequent_spotify_poll_is_clamped_not_fatal(tmp_path) -> None:
    """The poll only moves a progress bar on a bedside display; polling harder
    counts against the account's rate limit and buys nothing."""
    cfg = load_config(
        write_env(tmp_path, SECRET + "SPOTIFY_ENABLED=true\nSPOTIFY_POLL_S=0.2\n")
    )
    assert cfg.spotify_poll_s == 1.0
    assert any("SPOTIFY_POLL_S" in w for w in cfg.warnings)


def test_spotify_validation_only_applies_when_it_is_enabled(tmp_path) -> None:
    """A leftover value under a disabled feature must not stop the server."""
    cfg = load_config(write_env(tmp_path, SECRET + "SPOTIFY_BITRATE=256\n"))
    assert cfg.spotify_enabled is False


def test_librespot_args_are_split_like_a_shell(tmp_path) -> None:
    """The escape hatch for an upstream flag rename, so it has to survive
    quoting the way the owner would type it."""
    cfg = load_config(
        write_env(
            tmp_path,
            SECRET + 'SPOTIFY_LIBRESPOT_ARGS=--device "My Speaker" --verbose\n',
        )
    )
    assert cfg.spotify_extra_args == ["--device", "My Speaker", "--verbose"]


def test_describe_never_prints_the_spotify_creds_path_contents(tmp_path) -> None:
    """describe() goes to the log at every startup; the creds directory holds a
    refresh token that is, in practice, permanent."""
    cfg = load_config(write_env(tmp_path, SECRET + "SPOTIFY_ENABLED=true\n"))
    assert "Jarvis" in cfg.describe()
    assert cfg.spotify_creds_dir not in cfg.describe()


def test_system_instruction_from_file(tmp_path, monkeypatch) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("You are a very terse clock.", encoding="utf-8")
    monkeypatch.setenv("GEMINI_SYSTEM_INSTRUCTION_FILE", str(prompt))
    cfg = load_config(write_env(tmp_path, "ECHO_SHARED_SECRET=a-long-enough-secret\n"))
    assert cfg.system_instruction == "You are a very terse clock."


def test_missing_system_instruction_file_is_fatal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_SYSTEM_INSTRUCTION_FILE", str(tmp_path / "nope.txt"))
    with pytest.raises(ConfigError):
        load_config(write_env(tmp_path, "ECHO_SHARED_SECRET=a-long-enough-secret\n"))


def test_describe_never_prints_the_key_or_the_secret() -> None:
    cfg = Config(
        shared_secret="super-secret-value", gemini_api_key="AIza-not-a-real-key"
    )
    described = cfg.describe()
    assert "super-secret-value" not in described
    assert "AIza-not-a-real-key" not in described


def test_env_example_lists_every_documented_var() -> None:
    """The committed template must stay in step with what load_config reads,
    and must never contain a real value."""
    root = Path(__file__).resolve().parents[2]
    text = (root / ".env.example").read_text(encoding="utf-8")
    for name in (
        "ECHO_HOST",
        "ECHO_PORT",
        "ECHO_SHARED_SECRET",
        "ECHO_HEARTBEAT_INTERVAL_S",
        "ECHO_DISPLAY_REQUIRE_SECRET",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "GEMINI_VOICE",
        "GEMINI_MEDIA_RESOLUTION",
        "SESSION_IDLE_TIMEOUT",
        "SESSION_MAX_DURATION",
        "SESSION_END_TURN_SILENCE_S",
        "POST_ANSWER_SILENCE_S",
        "WAKE_ENABLED",
        "WAKE_MODEL",
        "WAKE_THRESHOLD",
        "WAKE_REFRACTORY_S",
        "WAKE_DOWNLOAD_MODELS",
        "VISION_MODE",
        "VISION_FPS",
        "VISION_JPEG_QUALITY",
        "MIC_GAIN",
        "WEATHER_ENABLED",
        "WEATHER_LAT",
        "WEATHER_LON",
        "WEATHER_POLL_S",
        "LOG_LEVEL",
        "RECORD_AUDIO_DIR",
        "SPOTIFY_ENABLED",
        "SPOTIFY_DEVICE_NAME",
        "SPOTIFY_LIBRESPOT_BIN",
        "SPOTIFY_CREDS_DIR",
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_REDIRECT_URI",
        "SPOTIFY_BITRATE",
        "SPOTIFY_INITIAL_VOLUME",
        "SPOTIFY_POLL_S",
        "SPOTIFY_CARD_PRIORITY",
        "SPOTIFY_ART_PROXY",
        "SPOTIFY_LIBRESPOT_ARGS",
    ):
        assert name in text, f"{name} is missing from .env.example"
    assert "GEMINI_API_KEY=\n" in text, "the template must ship an EMPTY key"
