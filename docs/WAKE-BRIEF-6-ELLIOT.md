# WAKE-BRIEF-6 — "Elliot" wake word (open source) + assistant identity

Goal: rename the assistant to **Elliot** — both the pre-session wake word and the
in-conversation identity. Picovoice Porcupine is REJECTED (commercial license
uncertainty for a product-like use). Open-source only.

## Part A — assistant identity (tiny, do first)
- The Gemini session prompt / system identity currently presents an unnamed or
  differently-named assistant (read `server/gemini_live.py` / `server/config.py`
  for the prompt construction). Give it the identity: the assistant is named
  **Elliot** — friendly, concise, dry-witted (keep whatever tone guidance
  exists). It should acknowledge the name when addressed mid-chat.
- Ideally make the identity string env-overridable (`ASSISTANT_IDENTITY` /
  `ASSISTANT_NAME` in `.env`, documented in `.env.example`) so renaming later is
  a config change, not a code change.
- Server tests must still pass. No client change needed for Part A.

## Part B — wake word "Elliot" (the real work)
Current: `server/wake.py` runs openWakeWord (`model_name` param, default
`alexa`; ONNX, downloads models on demand; never crashes boot on missing
models). openWakeWord has NO "elliot" model. Open-source plan:

### Primary: Vosk keyword spotting (Apache-2.0, offline)
- Add a Vosk backend to `wake.py` (or a sibling module behind the same
  interface — keep `WAKE_ENABLED` semantics and the no-model-no-crash boot
  rule). Vosk = `vosk` pip package + small English model (~40–50 MB,
  `vosk-model-small-en-us-0.15`) downloaded to a cache dir under the server.
- Pipeline: VAD-gated — only run inference while speech is present (the code
  already has a VAD path in `gemini_live.py` — reuse its gating idea so idle
  CPU stays low). On speech: feed chunks, take partial hypotheses, match a
  tolerance list: {"elliot", "elliott", "eliot", "yelliot"} — accept when the
  last ~0.8 s of partial text ends in a match (or a whole-utterance match) and
  require a cooldown (~1.5 s) before re-trigger.
- Config: `WAKE_ENGINE=vosk|openwakeword` (default keep current engine until
  Vosk is proven live), `WAKE_WORD=elliot`, `WAKE_MODEL_DIR`, Vosk model path.
  Log which engine is active at boot (the log line currently prints
  `models=['alexa']`).
- Sensitivity: expose `WAKE_THRESHOLD`-ish knob if the implementation allows;
  default chosen so the word triggers at normal speaking distance without
  constant false positives. Calibration notes in the commit message.

### Fallback (if Vosk accuracy disappoints in live testing): sherpa-onnx
- `sherpa-onnx` (k2-fsa, Apache-2.0) keyword spotting / wake word. Do NOT
  implement now — note in code comments + this brief as the fallback, and keep
  the `wake.py` interface engine-agnostic so swapping is contained.

### NOT doing
- No Gemini-as-wake-word (24/7 API streaming = cost + latency; defeats the
  local-wake design). No Porcupine/Picovoice (license). No openWakeWord custom
  training (dataset collection burden, uncertain quality).

## Scope rules
- `server/wake.py` + config + `.env.example` + tests. Engine-agnostic interface
  preserved. Do NOT touch WSL / ROM build. Do NOT disturb the ambient-UI work
  (UI-BRIEF-4) or the chat-end work (UI-BRIEF-5) beyond what is required.
- Client unchanged for Part B; Part A needs no client change. If a client label
  ever hardcodes "Alexa"/wake naming, list it in the report (do not redesign).

## Deliverable + verification
- Commits with clear messages. `.venv/Scripts/python.exe -m pytest server/ -q`
  green on Windows.
- Report (~15 lines): engine + model chosen, real CPU cost measured during a
  60 s idle listen and during speech (rough % via Windows perf if available),
  tolerance list, how mid-chat naming works, commit SHA(s). Do not claim done
  unless tests pass and the server boots with `WAKE_ENGINE=vosk` logging
  `models=['elliot']` (or equivalent).
