# UI-BRIEF-15 — Music focus: now-playing hero layout (auto-reorganize for music)

Repo: echo-gemini. Read `docs/HANDOFF.md`, `docs/SPOTIFY.md`,
`docs/SPOTIFY-BRIEF-12.md` (librespot player, v1.5 client protocol:
`media_control` messages, `now_playing` display card) and
`docs/UI-BRIEF-14-LAYOUT-ENGINE.md` (the focus engine this fills in) first.

**Prerequisites:**
1. Spotify brief-12 **finish + end-to-end verified** (audio on the Show);
2. UI-BRIEF-14 landed (focus templates + transitions exist).

## Goal

When music is playing or paused, the display auto-reorganizes: **music becomes the
hero** (big now-playing panel with controls), **the clock shrinks to a compact
corner** — the exact "music is the main thing, time smaller" behavior the owner asked
for. When playback stops or clears, it returns home.

## Behavior

1. **focus = music** when the player is playing OR paused (a paused track is still the
   current surface — don't bounce to home mid-song). Stop/clear → back to home focus
   (or chat if a session is somehow active).
2. **Music template:** hero panel with:
   - album art (lazy-loaded, bounded: fail to a monogram/title tile on timeout; don't
     blow the 1 GB budget — decode once, cap resolution),
   - title + artist (now_playing payload), live progress bar + times,
   - media row: prev / toggle / next — these fire the **existing v1.5 `media_control`**
     messages (toggle/pause/resume/next/previous). Taps on controls are MEDIA taps,
     never talk.
   - clock compacts to the corner block from UI-BRIEF-14; widget rail hidden or slim
     (design choice: keep weather mini or nothing — pick one, keep it consistent).
3. **Voice arbitration** (existing rule: conversation pauses music): a session
   starting flips focus to `chat` (and the player pauses); when the session ends and
   playback resumes → focus back to `music`; if the user stopped the music meanwhile →
   `home`. Server owns this state machine.
4. Layout engine integration: UI-BRIEF-14's `music` template stub gets filled here;
   focus transitions reuse the same ≤ 400 ms opacity/transform rules.

## Server work

- Extend focus computation with player state events (play/pause/stop/track change —
  wherever the poller/supervisor in `server/spotify.py` lands state) so `layout
  {focus: music}` pushes at the right moments. Unit-test the arbitration:
  playing→chat flip, resume→music return, stop→home, pause-stays-music.
- Confirm now-playing pushes are coherent with the media row (no double sources of
  truth between the now_playing card and the layout hero — the hero IS the surface now;
  suppress the old full-bleed now_playing card overlay while focus=music so the two
  don't stack).

## Client work

- WebView: music template in the layout engine (art, title/artist, progress, media
  row wired to the existing MediaControl bridge path), compact-clock variant,
  clean-up on clear/stop pushes (progress timer stops when paused/cleared — no
  zombie intervals on a 1 GB device).

## Scope rules

- No new protocol message types beyond what UI-BRIEF-14 + v1.5 already define; if the
  client needs a "player state changed" push that doesn't exist yet, add it minimally
  and say so. No Compose. Don't touch librespot internals beyond reading state.
- One worker per repo; commit in slices (server focus rules + tests, client template).

## Deliverable + verification

- `pytest server/ -q` green; `./gradlew assembleDebug` green (JAVA_HOME Adoptium 17).
- On-device: "play some lofi" → focus=music hero with art + controls, clock compact;
  pause via screen tap → stays music; next via screen tap → track updates; "hey
  jarvis …" → chat focus + music pauses; end session → music resumes, focus music;
  "stop the music" → focus home. Watch frame drops on art decode.
- Report ~15 lines + commit SHAs.
