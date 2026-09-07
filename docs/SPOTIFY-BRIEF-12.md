# SPOTIFY-BRIEF-12 — "Jarvis, play..." + media controls (owner: Premium confirmed 2026-09-06)

Goal: voice-controlled Spotify playback on the Show. "Hey Jarvis, play some
lofi" → music from the Show's speaker, with pause/skip/volume by voice, and a
now-playing card on the ambient UI. Media can also be controlled from Jaiden's
phone via Spotify Connect (librespot shows up as a device named "Jarvis").

## Architecture (from docs/SPOTIFY.md feasibility — this implements its spike)

- **librespot** (open-source Connect client) runs on the Windows PC as a
  subprocess of echo-server, `--backend pipe`, outputting PCM.
- Server receives PCM, converts to the device's audio format (24 kHz mono
  PCM16 — current AudioPlayback contract; 44.1 kHz stereo → 24 kHz mono
  resample), and streams it down the EXISTING device link audio channel. The
  Show stays a thin speaker+display — no Spotify code, no DRM, no GApps on the
  32-bit device.
- Credentials: Jaiden's Premium account. librespot auth: use the public
  librespot client-id with a one-time browser login (Hermes will hand Jaiden the
  URL when first enabled; code must store creds in a gitignored dir under
  `server/spotify_creds/`). `.env`: `SPOTIFY_ENABLED`, `SPOTIFY_DEVICE_NAME`
  (default "Jarvis").

## Server pieces

1. `server/spotify.py` — librespot subprocess manager (spawn on enable, watch
   exit, restart w/ backoff, clean shutdown). Windows binary path configurable
   (`SPOTIFY_LIBRESPOT_BIN`, default `tools/librespot.exe` — Hermes will fetch
   the Windows build separately). Pipe stdout → resampler → device audio sink,
   reusing the same down-channel the server already uses for Gemini audio if
   the codebase has one (check how the device AudioPlayback is fed; otherwise
   add a parallel sink + keep the mixer decision simple).
2. Resampler: pure-Python 44100 stereo PCM16 → 24000 mono PCM16 (average
   channels; linear/box resample ~183:100). Quality bar: "good enough for a
   bedside speaker", not audiophile. Must not block the event loop (run in a
   thread or chunked).
3. Gemini tools (mirror the existing alarm-tool wiring): `spotify_play(query)`
   (search + start), `spotify_pause`, `spotify_resume`, `spotify_next`,
   `spotify_previous`, `spotify_set_volume(0-100)`, `spotify_status`. All return
   short confirmations ("playing x").
4. now-playing pushes: on play/change + every ~5 s while playing, push
   `now_playing` (title/artist/album/art_url/progress_s/duration_s/is_playing —
   the protocol already has this card type) so the ambient WebView can render
   it.
5. Audio arbitration v1 (keep simple): starting a voice/Gemini session PAUSES
   music; when the session ends, RESUME if it was playing before the session.
   Chimes/alarms: alarms play over/duck music (alarm wins; music pauses for the
   alarm duration). Document any limiter.
6. Tests: unit-test the tool layer + resampler with fakes (no real librespot in
   CI). `pytest server/ -q` green.

## Client

No client code expected beyond the existing `now_playing` card rendering in the
ambient WebView (brief 9/10 already render NOW_PLAYING display types — verify;
if the ambient doesn't render album art yet, add a minimal now-playing overlay
that shows art + title + a pause/skip touch row that sends the control through
the bridge to the server).

## Sequence / deps

- Hermes fetches the librespot Windows binary + verifies `--backend pipe`
  emits PCM before this brief's code is testable end-to-end.
- Jaiden does the one-time browser login (Hermes hands the URL).
- Deliverable: voice "play X" works on the Show; phone-Connect control appears
  as a side effect.

## Verification (end-to-end, with Jaiden)

"Hey Jarvis, play lofi" → audio from Show, now-playing card visible; "pause",
"next", "volume to 40"; phone Spotify app shows "Jarvis" device; end of
Gemini chat resumes music.
