# Spotify

"Hey Jarvis, play some lofi" plays music out of the Show, with voice transport
controls, a now-playing card on the ambient screen, and the same speaker
controllable from the phone's Spotify app. Implements SPOTIFY-BRIEF-12; this
file replaces the feasibility note that preceded it.

Requires Spotify **Premium**: Connect playback is Premium-only, and everything
here is Connect playback.

## Shape

    Spotify  --Connect-->  librespot.exe  --stdout PCM-->  PcmConverter
    (Web API decides)      (makes the noise)                    |
                                            24 kHz mono PCM16 <-+
                                                                |
                                             Channel.AUDIO_MUSIC +--> the Show

librespot runs on the Windows PC as a subprocess of echo-server, registers a
Connect device called "Jarvis", and writes 44.1 kHz stereo PCM16 to stdout. The
server resamples that to the 24 kHz mono the device's playback is pinned to and
sends it down the WebSocket the device already holds.

The Show stays a thin speaker and display: no Spotify client, no DRM, no Google
services on a 32-bit device with under a gigabyte of RAM.

**librespot cannot be told anything.** It is a Connect *receiver*. Search,
play, pause, skip, volume and "what is playing" are all Spotify Web API calls
aimed at that device (`server/spotify_api.py`). Which is also why controlling
the music from a phone works with no code for it: the phone talks to the same
Connect device.

| Where | What |
|---|---|
| `server/spotify.py` | librespot supervisor, arbitration, now-playing pushes, the tool surface |
| `server/spotify_api.py` | the Web API calls, and only those |
| `server/spotify_auth.py` | OAuth (PKCE) and the token file |
| `server/resample.py` | 44.1 kHz stereo -> 24 kHz mono PCM16 |
| `server/tools/spotify_login.py` | the one-time browser login |

## Music is not on the voice audio channel

Protocol v1.5 adds `Channel.AUDIO_MUSIC` (0x04) rather than reusing
`AUDIO_DOWN`, which carries the same format to the same speaker. The reason is
on the device: `AUDIO_DOWN` feeds `AudioPlayback`, and `AudioPlayback` means
*the assistant is talking*. It reports speaking state, which closes the
microphone uplink (half-duplex, HANDOFF 8.2), and barge-in flushes it.

Music on that channel would therefore hold the microphone shut for as long as
it played — no wake word, so no way to say "pause" — and the first thing the
model said would flush the album.

So music gets `MusicPlayback`, its own `AudioTrack` tagged `USAGE_MEDIA`, and
Android mixes the two. Whether they are ever audible at once is a *server*
decision, below.

## Arbitration (v1, deliberately blunt)

One speaker, three claimants, resolved by named holds rather than by mixing.
A hold pauses Spotify and mutes the PCM stream; the last hold released resumes,
but only if something was playing when the first was taken.

* **A voice session** holds for as long as it is open. Music stops when the
  wake word fires and comes back when the conversation ends.
  Ducking was rejected: one weak microphone with no echo cancellation loses the
  question to music at any level under it.
* **An alarm or timer** holds while it is ringing, released when the device
  reports nothing ringing. The alarm wins outright — it is the one sound in the
  house with a deadline. The hold expires after ten minutes regardless, so a
  device that drops off Wi-Fi mid-alarm cannot leave the music paused forever.
* Both at once reference-count correctly: an alarm during a conversation does
  not resume the music when only the conversation ends.

Three interactions are worth knowing because they are what make it feel right
rather than merely correct:

* `spotify_play` and `spotify_resume` **drop the session's hold**. Otherwise
  "play some lofi" is honoured on Spotify's side and muted here until the
  session closes, so the answer is eight seconds of silence and then music.
* `spotify_pause` **clears the remembered "it was playing"**. The session
  remembers that music was on when the wake word fired; without this it would
  use that to undo an explicit "pause" eight seconds later.
* A hold that cannot reach Spotify still mutes the stream, so the conversation
  is audible either way, and never raises into the session lifecycle.

### Limiters

* A hold *pauses*, so a hold taken during a live radio stream resumes at the
  live edge rather than where it stopped. That is Spotify's behaviour.
* Volume is applied by librespot's software mixer and is a Connect property
  shared with every other client on the account — "volume to 40" changes what
  the phone shows too.
* The now-playing card is pushed only when playback is on **this** speaker.
  Music the owner started on their phone is genuinely playing, but a card on the
  bedside display would claim the display is playing it.

## Setting it up

1. **Get librespot.** Download the Windows build and put it at
   `tools/librespot.exe`, or point `SPOTIFY_LIBRESPOT_BIN` at it. Confirm
   `librespot.exe --backend pipe` writes PCM to stdout before expecting audio:
   upstream renames options between minor releases, `--access-token` and
   `--format S16` in particular. `SPOTIFY_LIBRESPOT_ARGS` appends extra flags
   verbatim if one needs overriding; the whole command line is built in one
   place, `LibrespotSupervisor.argv`.

2. **Log in once.**

       .venv\Scripts\python.exe server\tools\spotify_login.py

   It prints a URL, waits for the redirect, and writes
   `server/spotify_creds/token.json` (gitignored). Authorization Code with
   PKCE against librespot's public client id — there is no client secret to
   store, and the consent screen names librespot, which is the truth about what
   is going to play the audio. The refresh token it saves is in practice
   permanent: **treat that directory as the account password.**

   This is a separate script rather than something the server does on demand
   because a server that can block on a human at the console is a server that
   hangs at boot on the morning nobody is watching. With no credentials,
   echo-server logs this command and carries on without music.

3. **`SPOTIFY_ENABLED=true`** in `.env`, and restart. See `.env.example` for
   the rest of the settings.

Nothing above is required for the clock, the voice path, alarms or the display
API. Every failure mode — no binary, no login, expired token, Spotify
unreachable — logs what to do and leaves the rest of the server untouched. The
music functions are not declared to the model at all when Spotify is off: a
model told it can play music on a server that cannot will say it is playing
something, and then nothing will happen.

## Checking it

`GET /health` carries a `spotify` block: whether the account is linked, whether
librespot is running, how many times it has been restarted, the last error, and
what is playing. It is the first place to look for "why is there no music".

`server/tools/fake_device.py` counts music frames and can press the transport
buttons, so the server half is testable with no hardware.

`pytest server/ -q` covers the tool layer, the arbitration rules, the card
pushes, the librespot supervisor (spawn, PCM, crash, restart, shutdown) and the
resampler — all against fakes. No real librespot, account or network in CI.

## What was NOT tested here

Everything downstream of the binary. No librespot build was run, so
`--backend pipe` has not been observed emitting PCM, the flag names have not
been confirmed against a real `--help`, and no audio has reached the Show. The
one-time browser login has not been performed. Latency, bandwidth and a long
playback/reconnect soak are all unmeasured.

## Alternatives, and why not

**Device-side Spotify APK.** Needs a supported armeabi-v7a build, sign-in
without GApps, and audio focus and kiosk navigation on 1 GB of RAM. The higher
resource risk, on the constrained end of the system.

**Spotify Web Player in a WebView.** Needs Widevine provisioning and supported
EME. No GApps makes DRM availability suspect — not proof it is absent, but
treat it as blocked until actual protected playback succeeds on this ROM. A
desktop browser succeeding predicts nothing about this Android WebView.
