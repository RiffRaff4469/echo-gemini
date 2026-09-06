# UI-BRIEF-5 — Smart end-of-chat (quick chats end fast, long chats still work)

Owner complaint: "after I ask something quick I walk away / talk to someone else
and it keeps listening. But sometimes I want a slightly longer chat."

## Current behavior (read the code first: `server/gemini_live.py` _watchdog,
idle_seconds, feed_audio/VAD path; `server/config.py`; `Protocol.kt`; client
tap handling in `MainActivity.kt`/`Link.kt`)
- Turn closes 1.2 s after user speech stops (`SESSION_END_TURN_SILENCE_S`) so the
  model can answer — fine.
- The billed session then stays open until `SESSION_IDLE_TIMEOUT` (default 120 s)
  of NO USER SPEECH — that's why it "keeps listening" after a quick question.
  Worse: while it idles, the live mic still streams to the model, so nearby
  conversation gets interpreted as new turns.
- `idle_seconds` resets ONLY on user VAD speech — NOT on model audio — so a
  naive timeout reduction would kill sessions mid-answer on long replies.

## Required behavior
1. **Session may never die mid-answer.** Track model-output activity (audio
   chunks received from the Live API) and count idle from the LAST activity in
   EITHER direction (or from the moment the model finishes speaking). Long
   answers always complete.
2. **Post-answer quiet window (new, env-tunable `POST_ANSWER_SILENCE_S`,
   default ~8 s).** When the model has finished its reply and no further user
   speech arrives within the window → cleanly end the session back to ambient
   (same end path as today: UI returns to clock/ambient, wake word re-arms).
   Quick question → answer → ~8 s → done. This also stops eavesdropping: after
   the window, friend-chatter is not streamed to the model.
3. **Longer chats when wanted:**
   - Speaking again within the window continues naturally (turn-based follow-ups).
   - **Tap-to-stay:** a screen tap while a session is active (listening/speaking/
     thinking or within the post-answer window) sends a lightweight `stay`
     envelope over the link and extends the window (+30 s per tap, debounced).
   - The wake word re-opens a fresh session instantly when one is not active
     (already true — keep it).
4. **Subtle client affordance (do NOT make it a big UI):** during the last ~3 s
   of a post-answer window, fade in a small muted hint near the status band —
   "tap to keep talking" (or a tiny countdown dot). Tapping anywhere = stay.
   Restyle nothing else; the ambient redesign owns the visuals.

## Scope rules
- Server: `server/gemini_live.py` (activity tracking + post-answer window +
  watchdog semantics), `server/config.py` (+ env key, document in `.env.example`),
  `server/main.py` if the `stay` envelope needs routing, protocol wire type in
  `server/protocol.py` if envelopes are typed there. Extend the server test
  suite (fake-device tests exist — follow their pattern).
- Client: `Link.kt` (send `stay` on tap; only when a session is active — do not
  fight the existing tap-to-talk trigger logic, read how taps are currently
  dispatched in `MainActivity.kt`), `Protocol.kt` (new envelope if needed),
  `StatusOverlay.kt`/`MainActivity.kt` (the small hint). The tap-to-talk overlay
  and push surface must keep working unchanged.
- NO Compose. Views/Canvas. armeabi-v7a, minSdk 30.
- Do NOT touch the ambient redesign work already committed (UI-BRIEF-4 changes)
  beyond what this behavior needs. Do NOT touch WSL / the ROM build.
- Do NOT change `SESSION_END_TURN_SILENCE_S` semantics (1.2 s turn close stays).

## Deliverable + verification
- Commits with clear messages (server behavior + tests first, then client).
- `.venv/Scripts/python.exe -m pytest server/ -q` (Windows) green.
- `./gradlew assembleDebug` in `EchoTerminal/` succeeds (JAVA_HOME =
  `C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot`, ANDROID_HOME =
  `C:\Users\jaide\Android\Sdk`).
- Report ~15 lines: what changed per file, chosen defaults, how mid-answer
  closing is prevented, how the tap path coexists with tap-to-talk, commit
  SHA(s). Do not claim done unless both suites pass.
