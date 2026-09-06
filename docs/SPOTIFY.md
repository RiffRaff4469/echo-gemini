# Spotify feasibility — plumbing only

This build adds a `now_playing` display card; it does not start or control playback.
The executable POST example is in `server/tools/test_display.py` (`CASES`).
Card payload: `title`, `artist`, `album`, `art_url`, `progress_s`, `duration_s`, `is_playing`.
Progress is a snapshot; a future integration must push metadata updates.

## Recommended spike: server-side librespot

Run librespot on the PC with a PCM pipe backend, forwarding audio to the device socket.
This keeps Spotify credentials and decoding work off the 32-bit, 1 GB display.
Requires Spotify Premium. Verify the Windows build actually includes a working pipe backend.
Upstream documents `--backend pipe`, `--device`, and selectable sample formats:
[librespot options](https://github.com/librespot-org/librespot/wiki/Options).
Verify OAuth/discovery authentication, credential storage, token renewal, and Connect discovery.
Verify Windows pipe behavior, buffering, reconnects, and shutdown without hung subprocesses.
Measure the PCM format; current device audio is 24 kHz mono PCM16, so resampling/mixing is needed.
Define music versus Gemini/chime priority, ducking, stop/flush, and microphone echo suppression.
Measure latency and bandwidth and run a long playback/reconnect soak before selecting this path.
Keep credentials out of git and out of the APK; no playback implementation is included here.

## Alternatives

Device-side Spotify APK: verify a supported armeabi-v7a build, sign-in without GApps,
memory use on 1 GB RAM, audio focus, and kiosk navigation; this is the higher resource risk.
Spotify Web Player inside WebView: verify Widevine provisioning and supported EME/WebView.
No GApps makes DRM availability suspect, not proof that Widevine is absent.
Treat this route as likely blocked until actual protected playback succeeds on this ROM.
Do not assume a desktop browser success predicts this Android WebView's capabilities.

Selection gate: working Windows PCM pipe plus reliable audio arbitration on the Show.
No Premium account, Spotify APK, DRM capability, or real playback was tested in this build.
