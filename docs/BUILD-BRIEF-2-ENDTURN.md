# Build Brief 2 — Turn-end signal fix (Gemini Live)

Verified defect found by the orchestrator during live API verification. Fix it, test it,
commit it. Keep the change tight.

## Context

Repo root: `C:\Users\jaide\projects\echo-gemini`. You built this server in your previous
run; the code is yours. A real `GEMINI_API_KEY` now lives in the repo-root `.env`
(gitignored — do not commit it, do not echo it, do not paste it into any file).

## Verified facts (orchestrator's live testing, 2026-09-03)

1. Live model id `gemini-2.0-flash-live-001` is STALE. Current available model:
   `gemini-3.1-flash-live-preview`. Already fixed in `server/config.py`
   (`DEFAULT_GEMINI_MODEL`) and `.env.example` — committed. Do not revert, do not "fix" again.
2. `types.LiveConnectConfig(response_modalities=["AUDIO"])` is required — already correct in
   `gemini_live.py`.
3. **Defect:** when audio is streamed up via `send_realtime_input(audio=...)`, the API does
   NOT end the user turn on silence. A session that received a full 5 s utterance stayed
   silent for 30+ s with zero model audio. The moment an explicit turn-end was sent
   (`session.send(input="", end_of_turn=True)`, i.e. clientContent with `turn_complete`),
   the model answered immediately (14 audio messages received).
4. Consequence for the product: as written, the server opens a Live session on wake, streams
   the user's speech, and then waits forever — the model never answers. This is the single
   most important path in the app. Fix it.

## Required fix

Add a trailing-silence turn-end to the Live session lifecycle in `server/gemini_live.py`
(and any glue in `server/main.py` that feeds device audio into it):

- Track the time of the last user-audio frame sent to the session.
- When no user audio has arrived for a trailing-silence window (~1.2 s is a sane default;
  make it a Config field, e.g. `SESSION_END_TURN_SILENCE_S`, default 1.2) AND the model is
  not currently speaking (no model audio received since the last user burst) AND at least
  one user-audio frame was sent since the last model turn — send an explicit turn-end
  (use `send_client_content` with `turn_complete=True`, or the equivalent non-deprecated
  call in google-genai 1.75; do not use the deprecated `session.send`).
- Do NOT send turn-end while the model is speaking (that is barge-in territory, handled
  elsewhere). Do NOT send turn-end if the user never actually spoke.
- Reset the timer on any new user audio frame and after any model audio / turn_complete.
- The tap-to-talk path must benefit from the same fix (it goes through the same session).

## Tests

Extend `server/tests/test_session.py` (or wherever the fake connector lives) so the fake
Live connector records `end_of_turn` clientContent sends, and assert:
- silence after a user utterance → exactly one turn-end sent;
- user audio resuming before the window elapses → no premature turn-end;
- model speaking → no turn-end until it finishes;
- no audio at all → no turn-end.

All existing tests must keep passing: `python -m pytest server/ -q` (venv: `.venv` at repo
root — use `.venv/Scripts/python.exe`).

## Verification (do this once, it bills the real key)

1. `pytest` green.
2. End-to-end once: start `server/main.py` (it will pick up the key + secret from `.env`;
   port 8766 is free — the canonical 8765 is occupied by a stale orphaned instance you left
   running earlier; leave that one alone). Then run
   `.venv/Scripts/python.exe server/tools/fake_device.py --url ws://127.0.0.1:8766/ws`.
   EXPECTED NOW: wake fires → server opens Live session → model audio comes back and the
   device transcript shows `model audio received: >0s in >0 chunks` (the bundled
   wake-sample.wav says "alexa" and the model answers it). If the model stays silent for
   >20 s after the utterance ends, the fix is incomplete — iterate.
3. Stop your test server instance when done.

## Constraints

- Secrets: never commit `.env` or any key. Never print the key.
- Do not touch `docs/FLASHING.md`, `docs/FLASHING-AGENT.md`, `docs/ROM-BUILD.md`,
  `docs/HANDOFF.md`, `server/config.py`, or `.env.example` (model default already correct).
- Do not reformat or refactor unrelated code.
- Commit as one focused commit, e.g. `fix: send explicit turn-end after user speech (Live API does not auto-end turns)`.

## Report

Short final report: what you changed, test counts, the fake-device transcript line showing
model audio received, commit hash. No more.
