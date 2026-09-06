# WAKE-BRIEF-6 — "Jarvis" wake word + identity (UPDATED 2026-09-06: name is JARVIS, not Elliot)

Owner renamed the assistant to **Jarvis** (2026-09-06). Everything below
reflects that.

## Status — mostly DONE via env, no code needed
- **Wake model:** `WAKE_MODEL=hey_jarvis` set in `.env` → server boot log
  confirms `openWakeWord ready: models=['hey_jarvis'] (onnx)`. Wake phrase =
  **"hey jarvis"** (openWakeWord's trained phrase). VERIFIED LIVE 16:18.
- **Identity:** `prompts/jarvis-system.txt` committed; `.env` sets
  `GEMINI_SYSTEM_INSTRUCTION_FILE=prompts/jarvis-system.txt`. Content: Jarvis is
  the personal voice assistant in Jaiden and Patrick's dorm room; main jobs
  alarms/timers, music, weather, trivia; brief conversational answers; camera
  view awareness; stays quiet while the two humans talk to each other (helps the
  eavesdrop complaint). VERIFIED LIVE (next chat uses it).
- **Voice:** `GEMINI_VOICE` env (default `Puck`). Owner picks from the prebuilt
  list; swap = .env + server restart. See open question below.

## Remaining (only if owner wants it)
1. **Plain "Jarvis" without "hey"** — openWakeWord has no plain-Jarvis model.
   If "hey jarvis" annoys, implement the Vosk keyword-spot backend from the
   earlier draft (VAD-gated, tolerance list {"jarvis", "jarvis?"}) behind the
   same `wake.py` interface (`WAKE_ENGINE=vosk|openwakeword`). Only build on
   request — stock model may be good enough.
2. **Threshold tuning**: `WAKE_THRESHOLD` / `WAKE_REFRACTORY_S` env exist;
   tune after live false-positive/false-negative reports.
3. Re-check any client/docs label that hardcodes "alexa" or the old name and
   update (the server log line prints the active model already).

## Open question for owner
Which prebuilt Gemini voice? Candidates on this model: Puck (current, light
male), Charon (deep male), Kore (male), Fenrir (lower male), Leda/Aoede
(female). Swapping = 5 s env change + restart; owner can audition and we cycle
until right. Default stays Puck until told otherwise.

## Scope rules (if the Vosk work happens)
- `server/wake.py` + config + `.env.example` + tests, engine-agnostic. NO WSL /
  ROM build touches. Sequence after UI-BRIEF-4/5 + HARDWARE-BRIEF-7 per the
  one-worker rule. Report CPU cost + tolerance list + commit SHAs.
