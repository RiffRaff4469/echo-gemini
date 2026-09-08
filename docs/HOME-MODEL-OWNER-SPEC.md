# HOME MODEL — owner spec 2026-09-08 (supersedes UI-BRIEF-14's content-driven focus)

Recorded from Jaiden's messages (voice). This is the arbitration + idle rules for
the home/layout engine.

## Rules
1. **The main home screen is the default and the resting surface.** The device
   sits on the clock home unless actively in a conversation or showing a
   full-screen page (alarms/timers/stopwatch, weather, cards).
2. **The time is ALWAYS visible during idle.** The clock hero never rotates
   away. Only the SIDE WIDGETS change/rotate. Exception: when the screen is
   showing real visuals (camera/photo/video content), the time may hide.
3. **Ambient switching only after idle.** After the device has sat idle for a
   little while it may START switching (rail rotation; future: photo ambient).
   The moment the owner needs it (wake word, mic button, touch, any session),
   it returns to the home screen instantly — like the original Echo Show.
4. Screen = UI only (brief 7 v2): taps never start chats; the mic button and
   wake word own talk.

## Consequences for the layout engine (UI-BRIEF-14/15)
- Layout focus arbitration (server): `chat` while a session runs; otherwise
  `home`. **Music does NOT displace the home** — the now-playing state is a
  compact widget/chip on the home (or an ambient-rail card once idle). The
  earlier music-focus takeover (UI-BRIEF-15) is shelved until the ambient
  rotation phase.
- The 65/35 home: left clock hero (never rotates) + right widget rail (rotates
  only after idle; widgets: weather mini, world clocks, alarm/timer/stopwatch
  chips, mute chip when muted, now-playing chip when playing, hint line).
- Idle-rotation timing + the rail rotation live client-side (WebView); any
  engagement resets the idle timer. Default idle threshold TBD (~2-5 min).

## Status
- Server arbitration: chat>music>home landed in `c5af7e1`; **music>home branch
  to be removed** per rule 4 (music = home + chip). TODO in the next slice.
- WebView home rebuild (clock hero fixed + rotating rail + idle timer): NOT
  built yet — the current static home stays until it lands.
