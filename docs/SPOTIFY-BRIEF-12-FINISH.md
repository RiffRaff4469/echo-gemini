# SPOTIFY-BRIEF-12 — FINISH + VERIFY (resume after session limit)

The main brief's work is committed but was cut off by a session limit. A previous
run created `server/spotify.py` (librespot subprocess manager + now-playing
mapping) and touched client files (Protocol/Link/MainActivity/AmbientWeb +
ambient assets) — all in commit "spotify: librespot player integration...".

## Your job
1. Audit the committed state against `docs/SPOTIFY-BRIEF-12.md`:
   - `server/spotify.py` — complete? (manager, resampler 44100-stereo →
     24000-mono, tools: play/pause/resume/next/previous/set_volume/status)
   - Gemini tool wiring in the server (how alarms tools are wired — mirror it)
   - now-playing pushes (every ~5 s while playing + on change)
   - audio arbitration: Gemini session pauses music, resumes after; alarms duck
   - client: NOW_PLAYING rendering in the ambient WebView (art/title + pause/skip
     touch row via the bridge)
   - `.env.example` / `.gitignore` updates; tests for the tool layer + resampler
2. Implement whatever's missing. Do NOT attempt to run a real librespot binary
   (Hermes handles fetching it + the one-time Spotify login).
3. `pytest server/ -q` green + `./gradlew testDebugUnitTest assembleDebug`
   green. Commit your work.

Report what was missing vs done in one short list.
