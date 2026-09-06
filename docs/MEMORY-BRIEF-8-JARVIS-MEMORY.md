# MEMORY-BRIEF-8 — Jarvis long-term memory

Owner approved (2026-09-06). Jarvis should remember across sessions. Without
this, every chat starts blank; with it, Jarvis feels continuous.

## Seed facts (owner-provided, verbatim intent)
- Jarvis is the **personal assistant to Jaiden and Patrick in their dorm room**.
- Meant mainly for **alarms, music, weather, trivia, and other stuff**.
Seed these as permanent identity/role memory (they are already the system
prompt — the memory layer must keep them in sync, see below).

## Design (server-side only; device stays thin)
1. **Storage:** small JSON store on the PC next to the server (e.g.
   `server/memory/jarvis.json` — gitignored runtime file; seed file committed as
   `server/memory/seed.json`). Structure:
   - `identity`: the fixed role block (from `prompts/jarvis-system.txt` —
     single source of truth; memory layer reads it, does not duplicate).
   - `facts`: durable facts about Jaiden and Patrick / the room / preferences,
     each with `text`, `added`, `source` (session date), capped (~50 entries,
     oldest evicted or summarized).
   - `summaries`: rolling per-day or per-topic summaries of what was discussed
     (cap ~20, oldest compacted).
2. **Injection:** on each new Gemini Live session, compose the system
   instruction = identity block + a compact "MEMORY" section (recent facts +
   last few summaries, trimmed to fit ~1500 chars). Env file stays the
   mechanism (`GEMINI_SYSTEM_INSTRUCTION_FILE` points at a generated file the
   memory module rewrites when facts change, or the server passes the composed
   string inline — pick the cleaner path given how config.py loads it).
3. **Extraction:** after each session ends, distill what's worth keeping using a
   cheap model call (the existing provider/gateway or Gemini flash non-live) or
   deterministic rules if a model call is overkill: new durable facts
   ("prefers X", "lives in dorm Y", "has class Z"), plus a 2-3 line session
   summary. Dedupe against existing facts; never store raw transcripts; never
   store audio. Keep it small — this is a 2-person dorm assistant, not a CRM.
4. **Privacy:** everything stays on the PC except what's already sent to Gemini
   in the session prompt. Nothing new leaves the machine. No Drivn business
   data (per owner's standing rule — Patrick's business chats stay out of
   personal assistant memory; the dorm-room context is fine).
5. **Forgetting:** facts get `valid_until`-style expiry support later if needed;
   v1 = cap + oldest-evict + owner can delete via a small admin command or by
   editing the JSON (document the path).

## Scope rules
- Server-only (`server/`): memory module + config + tests + seed file.
  Client unchanged (UI may later show "remembered" hints — NOT in this pass).
- Sequence after UI-BRIEF-4/5/6/7 per one-worker rule. NO WSL / ROM touches.
- If any UI-BRIEF-4/5/6/7 code conflicts (e.g. config.py prompt loading was
  touched), adapt to the merged state — the identity file is now
  `prompts/jarvis-system.txt`; do not fork it.

## Deliverable + verification
- Commits with clear messages. Server suite green (`.venv/Scripts/python.exe -m
  pytest server/ -q`).
- Live check: chat with Jarvis ("my name is Jaiden" / "remind me I have a quiz
  Thursday"), end session, start a new one, ask "do you remember me?" → Jarvis
  recalls the seeded identity + the new fact. Report ~15 lines + commit SHAs.
